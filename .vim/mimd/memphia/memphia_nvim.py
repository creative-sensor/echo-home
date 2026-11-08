#!/usr/bin/env python
"""
Headless Memphia backend for the Neovim frontend (memphia.lua).

Runs the agent from memphia_core and speaks newline-delimited JSON over stdin/stdout.
stdout is reserved for the protocol; anything else printed goes to stderr.

backend -> nvim
  {"t":"style","id":N,"fg":"#rrggbb"|null,"bg":...,"bold":b,"italic":b,"underline":b,"reverse":b}
  {"t":"w","b":<buffer key>,"s":[[text, style id (0 = default)], ...]}   append to a buffer
  {"t":"clear","b":<buffer key>}
  {"t":"state", busy, phase, model, graph, pinned, cwd, steer:[..], tasks:[..], approval:{kind,detail,editing}|null,
   iface:{joined,connected,name,hub,state,members}|null}
  {"t":"focus","tab":<right tab key>}
  {"t":"input","text":...}            put text in the input line (approval edit mode)
  {"t":"exit"}

nvim -> backend
  {"t":"input","text":...} | {"t":"interrupt"} | {"t":"cancel"} | {"t":"clear","b":key} | {"t":"quit"}
  {"t":"net","op":"join","name","purpose","hub","network"} | {"t":"net","op":"leave"}   (from the inet.lua driver)
"""
import json
import os
import re
import sys
import threading
import time
from typing import Dict, List, Optional

# Claim the real stdout for the protocol before anything can print to it.
_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def _script_dir() -> str:
    """Directory of this file, as a native path (Git Bash/MSYS gives /c/... on Windows)."""
    d = os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        m = re.match(r"^[\\/]([a-zA-Z])(?:[\\/]|$)(.*)", d)  # \c\Users\... or /c/Users/...
        if m and not os.path.isdir(d):
            d = m.group(1).upper() + ":\\" + m.group(2)
        d = os.path.normpath(d)
    return d


_HERE = _script_dir()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import memphia_core as core  # noqa: E402
except ImportError as e:
    print(f"memphia_nvim: cannot import memphia_core: {e!r}\n"
          f"  python   : {sys.executable} {sys.version.split()[0]}\n"
          f"  __file__ : {__file__}\n"
          f"  script dir: {_HERE} (exists={os.path.isdir(_HERE)}, "
          f"core={os.path.isfile(os.path.join(_HERE, 'memphia_core.py'))})\n"
          f"  sys.path : {sys.path[:4]}", file=sys.stderr)
    raise

KEYS_HELP = """\033[1mKeys\033[0m  F1-F6/Tab: right pane │ wheel, PgUp/PgDn, Shift-↑/↓ (from input): scroll │ Ctrl-O: scroll target │ F12: follow
      Ctrl-T: hide/show agent pane │ F8: hide/show IFACE pane │ Ctrl-L: clear right pane │ Ctrl-X: interrupt LLM │ Ctrl-C: cancel task │ :MemphiaQuit
      Enter on a GRAPH [#] / UVIAN #n entry: load it into the LLM context (Shift-Enter: and interrupt)"""

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_BASIC = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5"]
_BRIGHT = ["#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]


def _xterm256(n: int) -> str:
    if n < 8:
        return _BASIC[n]
    if n < 16:
        return _BRIGHT[n - 8]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


class SgrState:
    """Current SGR attributes of one buffer; styles carry across writes like a terminal."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.fg: Optional[str] = None
        self.bg: Optional[str] = None
        self.bold = self.italic = self.underline = self.reverse = False

    def apply(self, params: str):
        codes = [int(c) if c else 0 for c in params.split(";")] if params else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 3:
                self.italic = True
            elif c == 4:
                self.underline = True
            elif c == 7:
                self.reverse = True
            elif c == 22:
                self.bold = False
            elif c == 23:
                self.italic = False
            elif c == 24:
                self.underline = False
            elif c == 27:
                self.reverse = False
            elif 30 <= c <= 37:
                self.fg = _BASIC[c - 30]
            elif c == 39:
                self.fg = None
            elif 40 <= c <= 47:
                self.bg = _BASIC[c - 40]
            elif c == 49:
                self.bg = None
            elif 90 <= c <= 97:
                self.fg = _BRIGHT[c - 90]
            elif 100 <= c <= 107:
                self.bg = _BRIGHT[c - 100]
            elif c in (38, 48) and i + 1 < len(codes):
                colour = None
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    colour = _xterm256(codes[i + 2] % 256)
                    i += 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    colour = "#%02x%02x%02x" % tuple(min(255, v) for v in codes[i + 2:i + 5])
                    i += 4
                if c == 38:
                    self.fg = colour
                else:
                    self.bg = colour
            i += 1

    def key(self) -> Optional[tuple]:
        k = (self.fg, self.bg, self.bold, self.italic, self.underline, self.reverse)
        return None if k == (None, None, False, False, False, False) else k


class Bridge:
    FLUSH_INTERVAL = 0.03  # batch streamed tokens into ~30 fps updates

    def __init__(self):
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()  # flusher thread and the final flush both write
        self.pending: List[dict] = []
        self.dirty = True
        self.sgr: Dict[str, SgrState] = {k: SgrState() for k in core.BUFFERS}
        self.styles: Dict[tuple, int] = {}
        self.agent: Optional[core.MemphiaAgent] = None
        self.running = True
        for buf in core.BUFFERS.values():
            buf.subscribe(self.on_write)
        core.set_redraw(self.mark_dirty)

    # ----- events from the core (any thread) -----
    def mark_dirty(self):
        self.dirty = True

    def emit(self, msg: dict):
        with self.lock:
            self.pending.append(msg)

    def _style_id(self, state: SgrState) -> int:
        key = state.key()
        if key is None:
            return 0
        sid = self.styles.get(key)
        if sid is None:
            sid = self.styles[key] = len(self.styles) + 1
            fg, bg, bold, italic, underline, reverse = key
            self.pending.append({"t": "style", "id": sid, "fg": fg, "bg": bg, "bold": bold,
                                 "italic": italic, "underline": underline, "reverse": reverse})
        return sid

    def on_write(self, key: str, text: Optional[str]):
        with self.lock:
            state = self.sgr[key]
            if text is None:
                state.reset()
                self.pending.append({"t": "clear", "b": key})
                return
            segs, pos = [], 0
            for m in _SGR.finditer(text):
                if m.start() > pos:
                    segs.append([text[pos:m.start()], self._style_id(state)])
                state.apply(m.group(1))
                pos = m.end()
            if pos < len(text):
                segs.append([text[pos:], self._style_id(state)])
            if not segs:
                return
            last = self.pending[-1] if self.pending else None
            if last and last["t"] == "w" and last["b"] == key:
                last["s"].extend(segs)
            else:
                self.pending.append({"t": "w", "b": key, "s": segs})

    # ----- output -----
    def state(self) -> dict:
        a = self.agent
        with a.lock:
            steer, tasks, ap = list(a.steering), list(a.tasks), a.approval
            loads = [f"{u}  {a.store.describe(u)}" for u in a.attachments]
        return {
            "t": "state", "busy": a.busy, "phase": a.phase, "model": core.MODEL_NAME,
            "graph": a.graph.enabled, "pinned": a.pinned_uuid, "cwd": core.to_bash_path(core.CURRENT_CWD),
            "steer": steer, "tasks": tasks, "loads": loads,
            "approval": {"kind": ap["kind"], "detail": ap["detail"], "editing": ap["editing"]} if ap else None,
            "iface": a.iface.summary() if a.iface else None,
        }

    def flush(self):
        with self.out_lock:
            with self.lock:
                batch, self.pending = self.pending, []
            if self.dirty and self.agent:
                self.dirty = False
                batch.append(self.state())
            for msg in batch:
                _PROTO.write(json.dumps(msg, ensure_ascii=False) + "\n")
            _PROTO.flush()

    def flusher(self):
        while self.running:
            time.sleep(self.FLUSH_INTERVAL)
            try:
                self.flush()
            except (BrokenPipeError, ValueError):
                self.running = False  # nvim went away
                if self.agent:
                    self.agent.shutdown()

    # ----- input from nvim (main thread) -----
    def serve(self, router: core.CommandRouter):
        a = self.agent
        for line in iter(sys.stdin.readline, ""):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "input":
                kind, text = router.accept(msg.get("text", ""))
                if kind == "keep":
                    self.emit({"t": "input", "text": text})
                elif kind == "exit":
                    break
            elif t == "interrupt":
                a.interrupt()
            elif t == "cancel":
                a.cancel()
            elif t == "net" and a.iface:
                if msg.get("op") == "join":
                    a.iface.join(msg.get("name") or None, msg.get("purpose") or None,
                                 msg.get("hub") or None, msg.get("network") or None)
                elif msg.get("op") == "leave":
                    a.iface.leave()
            elif t == "clear" and msg.get("b") in core.BUFFERS:
                core.BUFFERS[msg["b"]].clear()
            elif t == "quit":
                break
            self.mark_dirty()


def main():
    bridge = Bridge()
    threading.Thread(target=bridge.flusher, daemon=True).start()
    agent = core.bootstrap(sys.argv[1:])
    if not agent:
        bridge.flush()
        sys.exit(1)
    bridge.agent = agent
    router = core.CommandRouter(agent, focus_tab=lambda key: bridge.emit({"t": "focus", "tab": key}),
                                keys_help=KEYS_HELP)
    core.announce(agent, ":MemphiaQuit")
    threading.Thread(target=agent.worker, daemon=True).start()
    try:
        bridge.serve(router)
    finally:
        agent.shutdown()
        bridge.emit({"t": "exit"})
        bridge.running = False
        try:
            bridge.flush()
        except Exception:
            pass
        agent.graph.close()


if __name__ == "__main__":
    main()
