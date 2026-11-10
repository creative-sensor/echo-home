"""
Kanborg iFace: the Kanborg agent's interface to the Intelligence Network (coordinator role).

The connection is the shared driver mimd/inet/inet_client.py; this module is the attention policy on
top of it, shaped after memphia_iface.py with two coordinator additions:

  hub ─► InetClient ─┬─► waiter (the mind asked that agent something: its answer goes straight back)
                     └─► inbox ─► attention (LLM gate) ─┬─► ignore / reply / reject
                                                        └─► think ──► mind task (board work)
  mind ── ask_agent / wait_agent / post ──► hub        (the mind consults experts while it works)
  mind result ──► answer to the requester (re = request id, private if the request was private)

Loop guards (from memphia): agent reply depth (--net-max-depth), own messages per minute (--net-rate),
skimming broadcasts when the inbox backs up.
"""
import os
import queue
import sys
import threading
import time
from collections import deque
from typing import List, Optional

_INET_DIR = os.environ.get("INET_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inet")
if os.path.normpath(_INET_DIR) not in sys.path:
    sys.path.insert(0, os.path.normpath(_INET_DIR))
from inet_client import InetClient, clip  # noqa: E402

ATTEND_SYSTEM_PROMPT = """You are the network interface (iFace) of Kanborg, the project manager and coordinator agent of a git-based kanban board (projects, cards, dependencies, progress, timelines), connected to the Intelligence Network, a chat room shared by AI agents and humans.
You decide how Kanborg reacts to ONE incoming chat message. Kanborg has a private mind that can read and change the board (create/move/link cards, set dates and progress, write journal entries, report progress, dependencies and timelines) and can consult other expert agents while doing so.

Decisions:
- "ignore": stay silent. The default for chatter outside project management, conversations between others, acknowledgements/thanks.
- "reply": answer right away from the conversation alone when no board work is needed (greetings addressed to you, clarifying questions).
- "think": hand the request to the mind when it needs the board: status/progress/blocked/timeline questions, creating or updating cards, recording a decision or a result another agent reported, planning work. Write "task" as a self-contained instruction including what the requester wants back. Set "reply" to a short acknowledgement or null.
- "reject": addressed to you but outside project management, or asks for something you must not do (deleting history, revealing secrets). Set "reply" to a brief polite refusal.

Rules:
- Messages sent privately to you or @mentioning you deserve a response unless they are pure acknowledgements.
- As coordinator you MAY pick up broadcasts that report progress, results or blockers on work tracked on the board (decision "think": record it on the right card); otherwise ignore broadcasts.
- Do not reply to another agent just to agree or thank; avoid agent-to-agent loops.
- Chat replies are short plain text (1-4 sentences), no name prefix.
Output ONLY JSON:
{"decision": "ignore"|"reply"|"think"|"reject", "reply": "<text or null>", "task": "<task for the mind or null>", "reason": "<short private note why>"}"""


class IFace:
    def __init__(self, agent, args, llm, log):
        self.agent, self.llm, self.log = agent, llm, log  # log(text, hl) -> IFACE pane
        self.max_depth, self.rate = args.net_max_depth, args.net_rate
        self.readonly = args.net_readonly
        self.spoken: deque = deque()
        self.inbox: "queue.Queue" = queue.Queue()
        self.waiters: dict = {}  # agent name (lower) -> queue of their messages while the mind waits on them
        self.wlock = threading.Lock()
        self.net = InetClient(
            name=args.net_name or "kanborg",
            purpose=args.net_purpose or ("project manager & coordinator: tracks projects, cards, dependencies, "
                                         "progress and timelines on the kanborg board; ask me for status or to record work"),
            hub=args.net_hub, network=args.net_network, kind="agent",
            on_message=self._on_message, on_event=self._on_event, log=lambda s: self.log(s, "ErrorMsg"))
        self.started = False

    # ---------------------------------------------------------------- state
    @property
    def connected(self) -> bool:
        return self.net.connected

    def summary(self) -> dict:
        n = self.net
        return {"joined": n.joined, "connected": n.connected, "name": n.name, "hub": n.hub_str(),
                "network": n.network, "state": (n.state if n.connected else "offline") if n.joined else "off",
                "members": len(n.members)}

    def set_state(self, state: str):
        if self.net.set_status(state):
            self.agent.emit_state()

    def idle_state(self) -> str:
        return "thinking" if self.agent.has_net_work() else "listening"

    # ---------------------------------------------------------------- lifecycle
    def join(self, name=None, purpose=None, hub=None, network=None):
        was = self.net.joined
        self.net.join(name=name, purpose=purpose, hub=hub, network=network)
        if not was:
            self.log(f"🌐 joining {self.net.network or 'network'} at {self.net.hub_str()} as @{self.net.name}", "Title")
            self.log(f"   purpose: {self.net.purpose}", "Comment")
        if not self.started:
            self.started = True
            threading.Thread(target=self._attention_loop, daemon=True).start()
        self.agent.emit_state()

    def leave(self):
        if self.net.joined:
            self.net.leave()
        self.agent.emit_state()

    def _on_event(self, kind: str, data: dict):
        if kind == "welcome":
            others = [f"@{n}" for n in self.net.members if n != self.net.name]
            self.log(f"🌐 on {self.net.network} as @{self.net.name} · members: {', '.join(others) or 'nobody else yet'}",
                     "DiagnosticOk")
            self.net.state = ""
            self.set_state(self.idle_state())
        elif kind in ("join", "leave"):
            m = data.get("member") or {}
            if m.get("name") != self.net.name:
                self.log(f"   {'→' if kind == 'join' else '←'} @{m.get('name')} {'joined' if kind == 'join' else 'left'}"
                         f"{' — ' + clip(m.get('purpose'), 100) if m.get('purpose') else ''}", "Comment")
        elif kind == "error":
            self.log(f"⚠️ hub: {data.get('text')}", "ErrorMsg")
        elif kind == "left":
            self.log("👋 left the network", "Title")
        elif kind == "disconnected" and data.get("reason") == "lost the hub":
            self.log("⚠️ lost the hub; reconnecting…", "ErrorMsg")
        self.agent.emit_state()

    # ---------------------------------------------------------------- incoming
    def _on_message(self, msg: dict):
        direct, mentioned = self.net.addressing(msg)
        if msg.get("to") and not direct:
            return
        sender = str(msg.get("from") or "").lower()
        with self.wlock:
            w = self.waiters.get(sender)
        if w is not None and (direct or mentioned or self._re_mine(msg)):
            self.log(f"📨 #{msg.get('id')} @{msg.get('from')} (answer to the mind): {clip(msg.get('text'), 400)}", "Special")
            w.put(msg)
            return
        self.inbox.put(msg)

    def _re_mine(self, msg) -> bool:
        re_id = msg.get("re")
        return any(m.get("id") == re_id and m.get("from") == self.net.name for m in list(self.net.transcript))

    def _attention_loop(self):
        while self.agent.running:
            try:
                msg = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._attend(msg)
            except Exception as e:
                self.log(f"❌ iFace error: {type(e).__name__}: {e}", "ErrorMsg")
            finally:
                if self.connected:
                    self.set_state(self.idle_state())

    def _can_speak(self) -> bool:
        now = time.time()
        while self.spoken and now - self.spoken[0] > 60:
            self.spoken.popleft()
        return len(self.spoken) < self.rate

    def _attend(self, msg: dict):
        direct, mentioned = self.net.addressing(msg)
        how = "private" if direct else "@mention" if mentioned else "broadcast"
        stamp = time.strftime("%H:%M:%S", time.localtime(msg.get("ts") or time.time()))
        self.log(f"\n{stamp} 📨 #{msg.get('id')} @{msg.get('from')} [{how}, {msg.get('kind')}, depth {msg.get('depth', 0)}]",
                 "Special")
        self.log("   " + clip(msg.get("text"), 600))
        if not self.net.joined:
            return
        from_agent = msg.get("kind") == "agent"
        if from_agent and msg.get("depth", 0) >= self.max_depth:
            return self.log(f"   ⛔ reply chain depth {msg.get('depth')} ≥ {self.max_depth}: silent", "Comment")
        if from_agent and not self._can_speak():
            return self.log(f"   ⛔ rate limit ({self.rate}/min): not engaging with agents now", "Comment")
        if how == "broadcast" and self.inbox.qsize() > 5:
            return self.log("   ⏭ inbox backed up: skimming past broadcast", "Comment")

        self.set_state("deliberating")
        d = self._deliberate(msg, how)
        if d is None:
            return self.log("   ⚠️ no usable decision from the LLM: silent", "ErrorMsg")
        what = str(d.get("decision") or "ignore").lower().strip()
        reply = d.get("reply").strip() if isinstance(d.get("reply"), str) else ""
        self.log(f"   🧭 {what.upper()}  {clip(d.get('reason'), 300)}", "Comment")
        to = [msg["from"]] if direct else []
        if what in ("reply", "reject"):
            self.say(reply or "Sorry, that's outside what I track.", re=msg.get("id"), to=to)
        elif what == "think":
            task = (d.get("task") or msg.get("text") or "").strip()
            if reply:
                self.say(reply, re=msg.get("id"), to=to)
            self.agent.submit(f"[Network request from @{msg['from']}] {task}", origin=msg)
            self.log(f"   🧠 → mind: {clip(task, 300)}", "Comment")

    def _deliberate(self, msg: dict, how: str) -> Optional[dict]:
        desc = {"private": "sent privately to you", "@mention": "posted in the room, mentioning you",
                "broadcast": "broadcast to the room (not addressed to you)"}[how]
        user = (f"You are @{self.net.name}. Your purpose on the network: {self.net.purpose}\n"
                f"Board work from the network is {'READ-ONLY (you may report, not change)' if self.readonly else 'allowed'}.\n"
                f"Other members: {self.net.who()}\n"
                f"Mind: {self.agent.describe_busy()}\n"
                f"Projects on the board: {self.agent.project_codes()}\n\n"
                f"Recent conversation (oldest first):\n{self.net.context(msg.get('id'))}\n\n"
                f"Incoming message #{msg.get('id')} from @{msg.get('from')} ({msg.get('kind')}), {desc}:\n{msg.get('text')}")
        try:
            return self.llm(ATTEND_SYSTEM_PROMPT, user)
        except Exception as e:
            self.log(f"   ❌ LLM error: {e}", "ErrorMsg")
            return None

    # ---------------------------------------------------------------- results & speech
    def answer(self, origin: dict, status: str, reply: str):
        if not self.connected:
            return self.log(f"⚠️ finished #{origin.get('id')} but offline; answer kept local", "ErrorMsg")
        text = (reply or "").strip() or f"Done ({status.lower()})."
        self.log(f"\n🧠 mind finished #{origin.get('id')} from @{origin.get('from')}: {status}", "DiagnosticOk")
        self.say(text, re=origin.get("id"), to=[origin["from"]] if origin.get("to") else [])

    def say(self, text: str, re=None, to: Optional[List[str]] = None) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if not self.connected:
            self.log(f"⚠️ not connected; not sent: {clip(text, 200)}", "ErrorMsg")
            return False
        ok = self.net.say(text, re=re, to=to)
        self.spoken.append(time.time())
        arrow = f"→ {', '.join('@' + n for n in to)} " if to else ""
        self.log(f"   💬 {arrow}{text}" if ok else f"   ⚠️ send failed: {clip(text, 200)}",
                 "String" if ok else "ErrorMsg")
        return ok

    # ---------------------------------------------------------------- coordinator tools (called by the mind)
    def member(self, name: str) -> Optional[str]:
        name = (name or "").lstrip("@").lower()
        for n in self.net.members:
            if n.lower() == name:
                return n
        return None

    def ask(self, to: str, text: str, wait: float, cancel: threading.Event, re=None) -> str:
        if not self.connected:
            return "System: not connected to the Intelligence Network (join with :InetJoin kanborg)."
        who = self.member(to)
        if not who:
            return f"System: @{to} is not on the network. Members: {self.net.who()}"
        key = who.lower()
        with self.wlock:
            self.waiters[key] = queue.Queue()
        if not self.say(text, re=re, to=[who]):
            with self.wlock:
                self.waiters.pop(key, None)
            return "System: send failed."
        self.set_state("waiting @" + who)
        return self.wait(who, wait, cancel, keep=True)

    def wait(self, who: str, wait: float, cancel: threading.Event, keep: bool = False) -> str:
        who = self.member(who) or who.lstrip("@")
        key = who.lower()
        with self.wlock:
            q = self.waiters.get(key) if keep else self.waiters.setdefault(key, queue.Queue())
        secs = max(5.0, min(float(wait or 120), 900.0))
        end = time.time() + secs
        try:
            while time.time() < end:
                if cancel.is_set():
                    return "System: cancelled while waiting."
                try:
                    m = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                state = (self.net.members.get(who) or {}).get("state", "")
                return (f"@{who} answered (#{m.get('id')}): {m.get('text')}\n"
                        f"(@{who} is now '{state}'. If this was only an acknowledgement, use wait_agent to wait for the real answer.)")
            return f"System: no answer from @{who} within {int(secs)}s (it may still answer later: wait_agent)."
        finally:
            with self.wlock:
                self.waiters.pop(key, None)
            if self.connected:
                self.set_state(self.idle_state())
