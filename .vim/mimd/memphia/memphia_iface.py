"""
Memphia iFace: Memphia's interface to the Intelligence Network.

The network connection itself is the shared driver mimd/inet/inet_client.py (InetClient); this
module is Memphia's *attention policy* on top of it.

        network (public)                                  agent (private)
  hub ─► InetClient ─► inbox ─► attention loop ─┬─► ignore    (stay silent)
                                  (LLM gate)     ├─► reply     (talk right away)
                                                 ├─► reject    (polite refusal)
                                                 └─► think ──► NetTask ──► private mind
                                                                             (MemphiaAgent.worker)
  hub ◄─ say ◄─ share decision ◄─ inbox ◄──────────── task result (status/answer/trail) ◄─┘

The mind never touches the network: it only receives tasks. Its results come back to the iFace
privately and the share step decides what (if anything) leaves the agent. Local tasks typed by
the operator can also be shared (autoshare) under a stricter "only if useful to the room" rule.
Every step is logged to the IFACE pane. Without a joined iFace the agent behaves as before.

Loop guards: agent-to-agent reply depth (--net-max-depth), messages per minute (--net-rate)
for traffic coming from other agents, and skimming of broadcasts when the inbox backs up.
"""
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from typing import List, Optional

import memphia_core as core
from memphia_core import BUFFERS, log, parse_llm_json

_INET_DIR = os.environ.get("INET_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inet")
if os.path.normpath(_INET_DIR) not in sys.path:
    sys.path.insert(0, os.path.normpath(_INET_DIR))
from inet_client import InetClient, clip  # noqa: E402  (shared network driver)

DIM, RST, RED, CYAN, GREEN, MAG = "\033[90m", "\033[0m", "\033[31m", "\033[36m", "\033[32m", "\033[35m"

ATTEND_SYSTEM_PROMPT = """You are the network interface (iFace) of an AI agent connected to the Intelligence Network, a chat room shared by AI agents and humans.
You decide how the agent reacts to ONE incoming chat message. The agent also has a private mind that can run shell commands on its machine, search and execute its Uvian memory entries, and investigate step by step. The mind's work is private: nothing reaches the network unless you say it.

Decisions:
- "ignore": stay silent. The default for broadcast chatter outside the agent's purpose, conversations between others, acknowledgements/thanks, and anything you have nothing useful to add to. Silence is fine.
- "reply": answer in the chat right away, from the conversation alone, when no real work is needed (greetings addressed to you, clarifying questions, short answers within your purpose).
- "think": hand the request to the private mind when answering needs real work (running commands, checking files or memory, investigating). Write "task" as a self-contained instruction for the mind including what the requester wants back. Set "reply" to a short acknowledgement or null.
- "reject": the message is addressed to you but is outside your purpose, or asks for something you must not do (destructive actions, revealing secrets or private data). Set "reply" to a brief, polite refusal.

Rules:
- Messages sent privately to you or @mentioning you deserve a response unless they are pure acknowledgements. Broadcasts only when clearly within your purpose and you add value.
- Do not reply to another agent just to agree, thank or restate; avoid agent-to-agent loops.
- Never reveal secrets, credentials, private file contents or your internal reasoning.
- Chat replies are short plain text (1-4 sentences). Do not prefix them with your own name.
Output ONLY JSON:
{"decision": "ignore"|"reply"|"think"|"reject", "reply": "<text or null>", "task": "<task for the mind or null>", "reason": "<short private note why>"}"""

SHARE_SYSTEM_PROMPT = """You are the network interface (iFace) of an AI agent on the Intelligence Network chat.
The agent's private mind just finished a piece of work. You decide what, if anything, to say about it on the network.
- If the work was requested from the network, answer the requester: the result and key facts, or why it failed.
- If nobody on the network asked for it, share only when it is clearly useful to the ongoing conversation or to the agent's stated purpose on the network. Otherwise stay silent (share=false).
- Share conclusions, not internals: no raw logs, no credentials or secrets, no private file contents beyond what answers the request, no memory uuids unless asked.
- Plain text, concise (at most about 8 short lines). Do not prefix with your own name.
Output ONLY JSON: {"share": true|false, "text": "<message or null>", "reason": "<short private note why>"}"""


def ilog(text: str = ""):
    BUFFERS["iface"].writeln(text)


class NetTask(str):
    """A mind task that came from the network; `origin` is the hub message that asked for it."""
    origin: dict = {}


class IFace:
    def __init__(self, agent):
        a = core.ARGS
        self.agent = agent
        self.autoshare = not a.net_no_autoshare
        self.max_depth, self.rate = a.net_max_depth, a.net_rate
        self.spoken: deque = deque()  # timestamps of own messages (rate limit)
        self.inbox: "queue.Queue" = queue.Queue()
        self.net = InetClient(
            name=a.net_name or "memphia",
            purpose=a.net_purpose or ("general assistant: can investigate with shell commands on "
                                      f"{socket.gethostname()} and recall its Uvian memory"),
            hub=a.net_hub, network=a.net_network, kind="agent",
            on_message=lambda m: self.inbox.put(("msg", m)), on_event=self._on_event,
            log=lambda s: ilog(f"{RED}{s}{RST}"),
        )
        self.started = False
        agent.task_listeners.append(self._on_task_done)

    # ---------------------------------------------------------------- state
    @property
    def name(self) -> str:
        return self.net.name

    @property
    def joined(self) -> bool:
        return self.net.joined

    @property
    def connected(self) -> bool:
        return self.net.connected

    @property
    def state(self) -> str:
        if not self.net.joined:
            return "off"
        return self.net.state if self.net.connected else "offline"

    @property
    def members(self) -> dict:
        return self.net.members

    def hub_str(self) -> str:
        return self.net.hub_str()

    def summary(self) -> dict:
        return {"joined": self.joined, "connected": self.connected, "name": self.name, "hub": self.hub_str(),
                "network": self.net.network, "state": self.state, "members": len(self.net.members)}

    def set_state(self, state: str):
        if self.net.set_status(state):
            core.redraw()

    def _idle_state(self) -> str:
        a = self.agent
        with a.lock:
            pending = isinstance(a.current_task, NetTask) or any(isinstance(t, NetTask) for t in a.tasks)
        return "thinking" if pending else "listening"

    # ---------------------------------------------------------------- lifecycle
    def join(self, name: Optional[str] = None, purpose: Optional[str] = None,
             hub: Optional[str] = None, network: Optional[str] = None):
        was = self.net.joined
        self.net.join(name=name, purpose=purpose, hub=hub, network=network)
        if not was:
            ilog(f"{CYAN}🌐 joining {self.net.network or 'network'} at {self.hub_str()} as @{self.name}{RST}\n"
                 f"{DIM}   purpose: {self.net.purpose}{RST}")
        elif purpose:
            ilog(f"{DIM}🎯 purpose: {self.net.purpose}{RST}")
        if not self.started:
            self.started = True
            threading.Thread(target=self._attention_loop, daemon=True).start()
        core.redraw()

    def leave(self):
        if not self.net.joined:
            ilog("ℹ️ not on the network")
            return
        self.net.leave()
        core.redraw()

    def _on_event(self, kind: str, data: dict):
        if kind == "welcome":
            others = [f"@{n}" for n in self.net.members if n != self.name]
            ilog(f"{GREEN}🌐 on {self.net.network or 'network'} ({self.hub_str()}) as @{self.name}{RST}  "
                 f"{DIM}members: {', '.join(others) or 'nobody else yet'}{RST}")
            self.net.state = ""
            self.set_state(self._idle_state())
        elif kind in ("join", "leave"):
            m = data.get("member") or {}
            if m.get("name") != self.name:
                verb = "joined" if kind == "join" else "left"
                ilog(f"{DIM}   {'→' if kind == 'join' else '←'} @{m.get('name')} {verb} "
                     f"({m.get('kind')}{': ' + clip(m.get('purpose'), 120) if m.get('purpose') else ''}){RST}")
        elif kind == "error":
            ilog(f"{RED}⚠️ hub: {data.get('text')}{RST}")
        elif kind == "left":
            ilog(f"{CYAN}👋 left the network{RST}")
        elif kind == "disconnected" and data.get("reason") == "lost the hub":
            ilog(f"{RED}⚠️ lost the hub; reconnecting…{RST}")
        core.redraw()

    # ---------------------------------------------------------------- attention loop
    def _attention_loop(self):
        while self.agent.running:
            try:
                kind, item = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "msg":
                    self._attend(item)
                elif kind == "done":
                    self._share(*item)
            except Exception as e:
                ilog(f"{RED}❌ iFace {kind} error: {type(e).__name__}: {e}{RST}")
            finally:
                if self.connected:
                    self.set_state(self._idle_state())

    def _can_speak(self) -> bool:
        now = time.time()
        while self.spoken and now - self.spoken[0] > 60:
            self.spoken.popleft()
        return len(self.spoken) < self.rate

    def _attend(self, msg: dict):
        direct, mentioned = self.net.addressing(msg)
        if msg.get("to") and not direct:
            return  # someone else's private exchange
        how = "private" if direct else "@mention" if mentioned else "broadcast"
        stamp = time.strftime("%H:%M:%S", time.localtime(msg.get("ts") or time.time()))
        ilog(f"\n{DIM}{stamp}{RST} 📨 {MAG}#{msg.get('id')} @{msg.get('from')}{RST} "
             f"{DIM}[{how}, {msg.get('kind')}, depth {msg.get('depth', 0)}]{RST}\n   {clip(msg.get('text'), 600)}")
        if not self.joined:
            return
        from_agent = msg.get("kind") == "agent"
        if from_agent and msg.get("depth", 0) >= self.max_depth:
            ilog(f"{DIM}   ⛔ agent reply chain depth {msg.get('depth')} ≥ {self.max_depth}: staying silent{RST}")
            return
        if from_agent and not self._can_speak():
            ilog(f"{DIM}   ⛔ spoke {len(self.spoken)}× in the last minute (limit {self.rate}): "
                 f"not engaging with agents for now{RST}")
            return
        if how == "broadcast" and self.inbox.qsize() > 5:
            ilog(f"{DIM}   ⏭ inbox backed up ({self.inbox.qsize()}): skimming past broadcast{RST}")
            return

        self.set_state("deliberating")
        decision = self._deliberate(msg, how)
        if decision is None:
            ilog(f"{RED}   ⚠️ no usable decision (LLM reply was not JSON): staying silent{RST}")
            return
        what = str(decision.get("decision") or "ignore").lower().strip()
        reply = decision.get("reply").strip() if isinstance(decision.get("reply"), str) else ""
        ilog(f"   🧭 {what.upper()}  {DIM}{clip(decision.get('reason'), 300)}{RST}")
        to = [msg["from"]] if direct else []

        if what in ("reply", "reject"):
            if what == "reject" and not reply:
                reply = "Sorry, that's outside what I'm here for."
            if reply:
                self.say(reply, re=msg.get("id"), to=to)
        elif what == "think":
            task = (decision.get("task") or msg.get("text") or "").strip()
            if reply:
                self.say(reply, re=msg.get("id"), to=to)
            nt = NetTask(f"[Network request from @{msg['from']}] {task}")
            nt.origin = msg
            busy = self.agent.busy
            self.agent.submit_task(nt)
            ilog(f"   🧠 → private mind{' (busy: queued)' if busy else ''}: {clip(task, 400)}")
            log(f"\033[38;5;44m🌐 [iFace → mind]\033[0m request #{msg.get('id')} from @{msg['from']} queued as a task")
        elif what != "ignore":
            ilog(f"{DIM}   (unknown decision '{what}': staying silent){RST}")

    def _deliberate(self, msg: dict, how: str) -> Optional[dict]:
        a = self.agent
        with a.lock:
            queued = len(a.tasks)
        mind = f"busy with: {clip(a.current_task, 200)}" if a.busy else "idle"
        desc = {"private": "sent privately to you",
                "@mention": "posted in the room, mentioning you",
                "broadcast": "broadcast to the room (not addressed to you)"}[how]
        user = (f"You are @{self.name}. Your purpose on the network: {self.net.purpose}\n"
                f"Other members: {self.net.who()}\n"
                f"Private mind: {mind}; {queued} task(s) queued\n\n"
                f"Recent conversation (oldest first):\n{self.net.context(msg.get('id'))}\n\n"
                f"Incoming message #{msg.get('id')} from @{msg.get('from')} ({msg.get('kind')}), {desc}:\n"
                f"{msg.get('text')}")
        try:
            return parse_llm_json(core.standard_llm_call(ATTEND_SYSTEM_PROMPT, user))
        except Exception as e:
            ilog(f"{RED}   ❌ LLM error: {e}{RST}")
            return None

    # ---------------------------------------------------------------- mind results
    def _on_task_done(self, task, result: dict):
        """Called from the mind's worker thread after every task (cancelled/failed included)."""
        if isinstance(task, NetTask) or (self.autoshare and self.connected):
            self.inbox.put(("done", (task, result)))

    def _share(self, task, result: dict):
        origin = task.origin if isinstance(task, NetTask) else None
        if not self.connected or (not origin and not self.autoshare):
            if origin:
                ilog(f"{RED}⚠️ mind finished #{origin.get('id')} but we are offline; result kept private{RST}")
            return
        label = f"request #{origin.get('id')} from @{origin.get('from')}" if origin else "local task"
        ilog(f"\n🧠 {GREEN}mind finished{RST} {label}: {result.get('status')}  {DIM}{clip(result.get('reason'), 300)}{RST}")
        self.set_state("deliberating")
        trail = "\n".join(f"- {s}" for s in (result.get("trail") or [])[-12:]) or "- (none)"
        private = (f"Task: {result.get('intent')}\nStatus: {result.get('status')}\n"
                   f"Conclusion: {result.get('reason')}\nAnswer: {result.get('answer') or '(none)'}\n"
                   f"Steps taken:\n{trail}")
        if origin:
            where = "privately" if origin.get("to") else "in the room"
            req = f"Requested on the network by @{origin.get('from')} (message #{origin.get('id')}, {where}):\n{origin.get('text')}"
        else:
            req = (f"Nobody on the network asked for this; it was a local task from your operator.\n"
                   f"Recent conversation:\n{self.net.context()}")
        user = (f"You are @{self.name}. Your purpose on the network: {self.net.purpose}\n\n{req}\n\n"
                f"[PRIVATE MIND RESULT — not visible to the network]\n{private}")
        try:
            d = parse_llm_json(core.standard_llm_call(SHARE_SYSTEM_PROMPT, user)) or {}
        except Exception as e:
            ilog(f"{RED}   ❌ LLM error: {e}{RST}")
            d = {}
        text = d.get("text") if isinstance(d.get("text"), str) else ""
        if d.get("share") and text.strip():
            ilog(f"   📤 SHARE  {DIM}{clip(d.get('reason'), 300)}{RST}")
            self.say(text, re=origin.get("id") if origin else None,
                     to=[origin["from"]] if origin and origin.get("to") else [])
        elif origin and not d:
            self.say(f"I looked into it but couldn't put an answer together ({str(result.get('status')).lower()}).",
                     re=origin.get("id"), to=[origin["from"]] if origin.get("to") else [])
        else:
            ilog(f"   🔒 KEEP PRIVATE  {DIM}{clip(d.get('reason'), 300)}{RST}")

    # ---------------------------------------------------------------- talking
    def say(self, text: str, re=None, to: Optional[List[str]] = None) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if not self.connected:
            ilog(f"{RED}⚠️ not connected; not sent: {clip(text, 200)}{RST}")
            return False
        ok = self.net.say(text, re=re, to=to)
        self.spoken.append(time.time())
        arrow = f"→ {', '.join('@' + n for n in to)} " if to else ""
        body = text.replace("\n", "\n      ")
        ilog(f"   💬 {GREEN}{arrow}{RST}{body}" if ok else f"{RED}   ⚠️ send failed: {clip(text, 200)}{RST}")
        return ok

    # ---------------------------------------------------------------- ===n commands
    HELP = """\033[1miFace (Intelligence Network)\033[0m
  ===n                     status              ===nj [name] [purpose]  join the network
  ===nl                    leave               ===ns <text>            say it (@name = point-to-point)
  ===np <purpose>          why I'm here        ===na on|off            autoshare finished local tasks
  ===nw                    who is on the network
  From Neovim: :InetJoin memphia / :InetLeave memphia use the profile in vimpage_inet (g:inet_agents)."""

    def command(self, rest: str):
        sub, _, arg = rest.strip().partition(" ")
        arg = arg.strip()
        if sub == "":
            s = self.summary()
            log(f"🌐 iFace: {'joined' if s['joined'] else 'not joined'}, "
                f"{'connected' if s['connected'] else 'disconnected'} to {s['network'] or 'network'} ({s['hub']}) "
                f"as @{s['name']} [{s['state']}], {s['members']} member(s), "
                f"autoshare {'on' if self.autoshare else 'off'}\n   purpose: {self.net.purpose}\n" + self.HELP)
        elif sub == "j":
            name, _, purpose = arg.partition(" ")
            self.join(name or None, purpose.strip() or None)
        elif sub == "l":
            self.leave()
        elif sub == "s":
            if not arg:
                log("ℹ️ Usage: ===ns <text>")
            else:
                self.say(arg)
        elif sub == "p":
            if arg:
                self.net.configure(purpose=arg)
                ilog(f"{DIM}🎯 purpose: {self.net.purpose}{RST}")
            else:
                log(f"🎯 purpose: {self.net.purpose}")
        elif sub == "a":
            if arg in ("on", "off"):
                self.autoshare = arg == "on"
            log(f"🌐 autoshare {'on' if self.autoshare else 'off'}")
        elif sub == "w":
            if not self.connected:
                log("ℹ️ not connected to the network")
            for n, m in self.net.members.items():
                log(f"  {'◆' if m.get('kind') == 'agent' else '●'} @{n}{' (me)' if n == self.name else ''} "
                    f"[{m.get('state')}] {clip(m.get('purpose'), 120)}")
        else:
            log(self.HELP)
