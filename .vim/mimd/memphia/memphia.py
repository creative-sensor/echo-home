#!/usr/bin/env python
"""
Memphia: interactive agentic shell with Uvian memory, a Memgraph relation graph
and a full-screen multi-buffer TUI. The engine lives in memphia_core.py; memphia.lua
(+ memphia_nvim.py) provides the same UI inside Neovim.

Layout
  left pane   : agent log (always visible, toggle with Ctrl-T)
  right pane  : one of LLM / STDOUT / STDERR / UVIAN / MAP-REDUCE / GRAPH (F1..F6 or Tab)
  queue panel : pending steering messages and queued tasks
  input line  : always active, even while the agent loop is running

Input routing
  idle  : text starts a new task
  busy  : text becomes a steering message injected at the next turn
          "!text" steers AND interrupts the current LLM generation immediately
          "+text" queues a new task to run after the current one
  approval pending : y / n / e (edit the command in the input line)

Commands: type ===h inside the TUI.
"""
import os
import sys
import threading
import time
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

import memphia_core as core
from memphia_core import BUFFERS, HISTORY_FILE, RIGHT_TABS, CommandRouter, MemphiaAgent

KEYS_HELP = """\033[1mKeys\033[0m  F1-F6/Tab: right pane │ PgUp/PgDn, Shift-↑/↓, wheel: scroll │ Ctrl-O: scroll target │ End(F12): follow
      Ctrl-T: hide/show agent pane │ Ctrl-L: clear right pane │ Esc: interrupt LLM │ Ctrl-C: cancel task │ Ctrl-Q: quit"""


class BufferView:
    """prompt_toolkit rendering of one OutputBuffer: the lines ending at its scroll target."""

    RENDER_LINES = 800  # lines handed to prompt_toolkit per frame (keeps redraws cheap)

    def __init__(self, key: str):
        self.buf = BUFFERS[key]
        self._key = None
        self._frags: list = []
        self._cursor = 0

    def formatted(self):
        if self.buf.view_key() != self._key:
            self._key, lines = self.buf.render_window(self.RENDER_LINES)
            self._frags = to_formatted_text(ANSI("\n".join(lines)))
            # Clamp to the lines actually produced so the cursor can never point past the content.
            n_lines = sum(f[1].count("\n") for f in self._frags) + 1
            self._cursor = max(0, min(len(lines), n_lines) - 1)
        return self._frags

    def cursor(self) -> Point:
        # Must match the fragments just returned by formatted(), even if another thread wrote since.
        return Point(x=0, y=self._cursor)


VIEWS = {key: BufferView(key) for key in BUFFERS}


class _ScrollableControl(FormattedTextControl):
    """Output control that scrolls its OutputBuffer with the mouse wheel."""

    WHEEL_LINES = 3

    def __init__(self, get_key: Callable[[], str], on_scroll: Callable[[], None], **kw):
        super().__init__(lambda: VIEWS[get_key()].formatted(),
                         get_cursor_position=lambda: VIEWS[get_key()].cursor(), **kw)
        self._get_key, self._on_scroll = get_key, on_scroll

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            BUFFERS[self._get_key()].scroll(-self.WHEEL_LINES)
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            BUFFERS[self._get_key()].scroll(self.WHEEL_LINES)
        else:
            return NotImplemented
        self._on_scroll()
        return None


class MemphiaTUI:
    def __init__(self, agent: MemphiaAgent):
        self.agent = agent
        self.right = 0
        self.show_agent_pane = True
        self.scroll_target = "right"
        self.router = CommandRouter(agent, focus_tab=self._focus_tab, keys_help=KEYS_HELP)

        self.input = TextArea(
            height=D(min=1, max=3), multiline=False, wrap_lines=True,
            prompt=self._prompt, history=FileHistory(os.path.join("./", HISTORY_FILE)),
            accept_handler=self._accept, style="class:input",
        )
        self.app = Application(
            layout=Layout(self._build_layout(), focused_element=self.input),
            key_bindings=self._bindings(), style=self._style(), full_screen=True,
            mouse_support=True, min_redraw_interval=0.05, refresh_interval=0.5,
        )

    # ----- layout -----
    def right_key(self) -> str:
        return RIGHT_TABS[self.right]

    def right_buf(self) -> core.OutputBuffer:
        return BUFFERS[self.right_key()]

    def _focus_tab(self, key: str):
        self.right = RIGHT_TABS.index(key)

    def _pane(self, get_key: Callable[[], str], header: Callable, target: str):
        def on_scroll():
            self.scroll_target = target  # wheel over a pane makes it the keyboard scroll target too
        return HSplit([
            Window(FormattedTextControl(header), height=1, style="class:header"),
            Window(_ScrollableControl(get_key, on_scroll, focusable=False, show_cursor=False),
                   wrap_lines=True),
        ])

    def _agent_header(self):
        b = BUFFERS["agent"]
        mark = "" if b.following else "  ⏸ scrolled"
        tgt = " ◀" if self.scroll_target == "agent" else ""
        return [("class:tab.active", " AGENT "), ("class:header", f"{mark}{tgt}")]

    def _right_header(self):
        frags = []
        for i, key in enumerate(RIGHT_TABS):
            b = BUFFERS[key]
            if i == self.right:
                b.unread = False
                frags.append(("class:tab.active", f" F{i + 1} {b.title} "))
            else:
                frags.append(("class:tab.unread" if b.unread else "class:tab", f" F{i + 1} {b.title}{'•' if b.unread else ''} "))
            frags.append(("class:header", " "))
        rb = self.right_buf()
        if not rb.following:
            frags.append(("class:header", " ⏸ scrolled"))
        if self.scroll_target == "right":
            frags.append(("class:header", " ◀"))
        return frags

    def _queue_text(self):
        a = self.agent
        with a.lock:
            steer, tasks = list(a.steering), list(a.tasks)
        frags = []
        for s in steer[:3]:
            frags.append(("class:q.steer", " STEER "))
            frags.append(("", f" {s[:200]}\n"))
        for t in tasks[:3]:
            frags.append(("class:q.task", " TASK  "))
            frags.append(("", f" {t[:200]}\n"))
        hidden = len(steer) + len(tasks) - min(3, len(steer)) - min(3, len(tasks))
        if hidden > 0:
            frags.append(("class:q.more", f" … {hidden} more queued\n"))
        return frags

    def _queue_height(self):
        a = self.agent
        with a.lock:
            n = min(3, len(a.steering)) + min(3, len(a.tasks))
            more = (len(a.steering) + len(a.tasks)) > n
        return D.exact(n + (1 if more else 0))

    def _status(self):
        a = self.agent
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 2) % 10] if a.busy else "●"
        state = "class:st.busy" if a.busy else "class:st.idle"
        g = "class:st.on" if a.graph.enabled else "class:st.off"
        return [
            (state, f" {spin} {a.phase} "),
            ("class:status", f" {core.MODEL_NAME} "),
            (g, " graph " if a.graph.enabled else " graph off "),
            ("class:status", f" 📌 {a.pinned_uuid or '-'} │ cwd {core.to_bash_path(core.CURRENT_CWD)} │ ===h help "),
        ]

    def _prompt(self):
        a = self.agent
        if a.approval:
            if a.approval["editing"]:
                return [("class:p.approve", " EDIT CMD "), ("", " ")]
            return [("class:p.approve", " ALLOW? y/n/e "), ("", " ")]
        if a.busy:
            return [("class:p.steer", " STEER "), ("class:p.hint", " !now +task "), ("", " ")]
        return [("class:llm", " MEMPHIA "), ("class:prompt", " Prompt!a "), ("", " ")]

    def _build_layout(self):
        agent_pane = self._pane(lambda: "agent", self._agent_header, "agent")
        right_pane = self._pane(self.right_key, self._right_header, "right")
        body = VSplit([
            ConditionalContainer(HSplit([agent_pane]), filter=Condition(lambda: self.show_agent_pane)),
            ConditionalContainer(Window(width=1, char="│", style="class:sep"), filter=Condition(lambda: self.show_agent_pane)),
            right_pane,
        ])
        queue = Window(FormattedTextControl(self._queue_text), height=self._queue_height, style="class:queue")
        return HSplit([
            body,
            queue,
            Window(FormattedTextControl(self._status), height=1, style="class:status"),
            self.input,
        ])

    def _style(self):
        return Style.from_dict({
            "header": "bg:#1c1c1c fg:#888888",
            "tab": "bg:#262626 fg:#a8a8a8",
            "tab.unread": "bg:#262626 fg:#c4c408 bold",
            "tab.active": "bg:#c4c408 fg:#000000 bold",
            "sep": "fg:#444444",
            "queue": "bg:#121212",
            "q.steer": "bg:#5f00af fg:#ffffff bold",
            "q.task": "bg:#005f87 fg:#ffffff bold",
            "q.more": "fg:#888888 italic",
            "status": "bg:#303030 fg:#d0d0d0",
            "st.idle": "bg:#005f00 fg:#ffffff bold",
            "st.busy": "bg:#af5f00 fg:#ffffff bold",
            "st.on": "bg:#5f00af fg:#ffffff",
            "st.off": "bg:#444444 fg:#999999",
            "llm": "bg:#c4c408 fg:#000000 bold",
            "prompt": "bg:#000000 fg:#c4c408",
            "p.steer": "bg:#5f00af fg:#ffffff bold",
            "p.hint": "fg:#808080",
            "p.approve": "bg:#af0000 fg:#ffffff bold",
        })

    # ----- keys -----
    def _scroll_buf(self) -> core.OutputBuffer:
        return BUFFERS["agent"] if self.scroll_target == "agent" and self.show_agent_pane else self.right_buf()

    def _bindings(self):
        kb = KeyBindings()

        @kb.add("c-q")
        def _(event):
            self.agent.shutdown()
            event.app.exit()

        @kb.add("c-c")
        def _(event):
            if self.agent.busy:
                self.agent.cancel()
            else:
                self.input.buffer.reset()

        @kb.add("escape", eager=True)
        def _(event):
            self.agent.interrupt()

        for i in range(len(RIGHT_TABS)):
            @kb.add(f"f{i + 1}")
            def _(event, i=i):
                self.right = i

        @kb.add("tab")
        def _(event):
            self.right = (self.right + 1) % len(RIGHT_TABS)

        @kb.add("s-tab")
        def _(event):
            self.right = (self.right - 1) % len(RIGHT_TABS)

        @kb.add("pageup")
        def _(event):
            self._scroll_buf().scroll(-max(5, event.app.output.get_size().rows - 8))

        @kb.add("pagedown")
        def _(event):
            self._scroll_buf().scroll(max(5, event.app.output.get_size().rows - 8))

        @kb.add("s-up")
        def _(event):
            self._scroll_buf().scroll(-1)

        @kb.add("s-down")
        def _(event):
            self._scroll_buf().scroll(1)

        @kb.add("f12")
        @kb.add("c-end")
        def _(event):
            self._scroll_buf().follow()

        @kb.add("c-o")
        def _(event):
            self.scroll_target = "agent" if self.scroll_target == "right" else "right"

        @kb.add("c-t")
        def _(event):
            self.show_agent_pane = not self.show_agent_pane

        @kb.add("c-l")
        def _(event):
            self.right_buf().clear()

        return kb

    # ----- input routing -----
    def _accept(self, buff) -> bool:
        kind, text = self.router.accept(buff.text)
        if kind == "keep":
            buff.text = text
            buff.cursor_position = len(text)
            return True
        if kind == "exit":
            self.app.exit()
        return False

    def run(self):
        core.set_redraw(self.app.invalidate)
        threading.Thread(target=self.agent.worker, daemon=True).start()
        self.app.run()


def main():
    agent = core.bootstrap()
    if not agent:
        sys.exit(1)
    tui = MemphiaTUI(agent)
    core.announce(agent, "Ctrl-Q")
    try:
        tui.run()
    finally:
        agent.shutdown()
        agent.graph.close()


if __name__ == "__main__":
    main()
