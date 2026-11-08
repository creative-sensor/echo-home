"""
inet_client: reusable Intelligence Network connection for agent backends (stdlib only, threaded).

An agent plugs in with its own params and gets the traffic through callbacks; what it does with
it (its "attention" policy) stays in the agent. Memphia's memphia_iface.py is one such agent.

    from inet_client import InetClient
    net = InetClient(name="scout", purpose="watches CI logs", hub="127.0.0.1:7788",
                     on_message=lambda msg: ..., on_event=lambda kind, data: ..., log=print)
    net.join()                       # connects in the background, reconnects until leave()
    net.say("hi @bee")               # @name = point-to-point (hub routing); to=[] broadcast; to=[..] explicit
    net.set_status("thinking")       # presence shown in every roster
    net.leave()

Callbacks run on the reader thread; keep them short (queue the work).
  on_message(msg)      every chat message from someone else (see inet_hub.py for the fields)
  on_event(kind, data) "welcome" | "members" | "join" | "leave" | "status" | "error" | "disconnected" | "left"

Wire protocol: inet_hub.py docstring.
"""
import json
import socket
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_HUB = "127.0.0.1:7788"


def parse_hub(spec: Optional[str]) -> Tuple[str, int]:
    host, _, port = (spec or DEFAULT_HUB).rpartition(":")
    return (host or "127.0.0.1", int(port) if port.isdigit() else 7788)


def clip(s, n: int = 300) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class InetClient:
    def __init__(self, name: str, purpose: str = "", hub: Optional[str] = None, kind: str = "agent",
                 network: str = "", on_message: Callable[[dict], None] = lambda m: None,
                 on_event: Callable[[str, dict], None] = lambda k, d: None,
                 log: Callable[[str], None] = lambda s: None, history: int = 40):
        self.name, self.purpose, self.kind = name, purpose, kind
        self.hub = parse_hub(hub)
        self.network = network          # expected/announced network name ("" until welcome)
        self.on_message, self.on_event, self.log = on_message, on_event, log
        self.joined = False             # wants to be on the network
        self.connected = False          # hub session established
        self.state = ""
        self.members: Dict[str, dict] = {}
        self.transcript: deque = deque(maxlen=history)  # messages visible to this member
        self._sock: Optional[socket.socket] = None
        self._wlock = threading.Lock()
        self._kick = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = True

    # ---------------------------------------------------------------- params
    def hub_str(self) -> str:
        return f"{self.hub[0]}:{self.hub[1]}"

    def configure(self, name: Optional[str] = None, purpose: Optional[str] = None,
                  hub: Optional[str] = None, network: Optional[str] = None):
        """Change params; name/hub only take effect on the next connection."""
        if network:
            self.network = network
        if hub and parse_hub(hub) != self.hub:
            if self.connected:
                self.log(f"ℹ️ already connected to {self.hub_str()}; leave first to switch to {hub}")
            else:
                self.hub = parse_hub(hub)
        if name and name != self.name:
            if self.connected:
                self.log(f"ℹ️ already on the network as @{self.name}; leave first to rejoin as @{name}")
            else:
                self.name = name
        if purpose and purpose.strip() != self.purpose:
            self.purpose = purpose.strip()
            self.send({"t": "profile", "purpose": self.purpose})

    # ---------------------------------------------------------------- lifecycle
    def join(self, **params):
        self.configure(**params)
        if self.joined:
            self._kick.set()
            return
        self.joined = True
        if not self._thread:
            self._thread = threading.Thread(target=self._conn_loop, daemon=True)
            self._thread.start()
        self._kick.set()

    def leave(self):
        if not self.joined:
            return
        self.joined = False
        self.send({"t": "bye"})
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.state = ""
        self._kick.set()

    def close(self):
        self.leave()
        self._running = False
        self._kick.set()

    # ---------------------------------------------------------------- talking
    def send(self, obj: dict) -> bool:
        with self._wlock:
            sock = self._sock
            if not sock:
                return False
            try:
                sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                return True
            except OSError:
                return False

    def say(self, text: str, re=None, to: Optional[List[str]] = None) -> bool:
        """to=None lets the hub route by @mentions; [] forces a broadcast; [names] is point-to-point."""
        text = (text or "").strip()
        if not text or not self.connected:
            return False
        obj = {"t": "say", "text": text, "re": re}
        if to is not None:
            obj["to"] = list(to)
        return self.send(obj)

    def set_status(self, state: str) -> bool:
        if state == self.state:
            return False
        self.state = state
        self.send({"t": "status", "state": state})
        return True

    # ---------------------------------------------------------------- reading helpers
    def addressing(self, msg: dict) -> Tuple[bool, bool]:
        """(sent privately to me, mentions me)."""
        me = self.name.lower()
        direct = any(n.lower() == me for n in msg.get("to") or [])
        mentioned = any(n.lower() == me for n in msg.get("mentions") or [])
        return direct, mentioned

    def format_msg(self, m: dict, width: int = 500) -> str:
        who = "you" if m.get("from") == self.name else f"@{m.get('from')}"
        to = m.get("to") or []
        arrow = f" → {', '.join('you' if t == self.name else '@' + t for t in to)} (private)" if to else ""
        return f"[#{m.get('id')}] {who}{arrow}: {clip(m.get('text'), width)}"

    def context(self, exclude_id=None, n: int = 20) -> str:
        msgs = [m for m in list(self.transcript) if m.get("id") != exclude_id][-n:]
        return "\n".join(self.format_msg(m) for m in msgs) or "(nothing yet)"

    def who(self) -> str:
        return ", ".join(f"@{n} ({m.get('kind')}{': ' + clip(m.get('purpose'), 80) if m.get('purpose') else ''})"
                         for n, m in self.members.items() if n != self.name) or "nobody else"

    # ---------------------------------------------------------------- connection loop
    def _conn_loop(self):
        backoff = 1
        while self._running:
            if not self.joined:
                self._kick.wait(0.5)
                self._kick.clear()
                continue
            try:
                sock = socket.create_connection(self.hub, timeout=5)
            except OSError as e:
                self.log(f"⚠️ network {self.network or ''} at {self.hub_str()} unreachable ({e}); retry in {backoff}s "
                         f"(bring it up with :InetUp)")
                self.on_event("disconnected", {"reason": str(e)})
                self._kick.wait(backoff)
                self._kick.clear()
                backoff = min(30, backoff * 2)
                continue
            backoff = 1
            sock.settimeout(None)
            with self._wlock:
                self._sock = sock
            self.send({"t": "hello", "name": self.name, "kind": self.kind, "purpose": self.purpose})
            try:
                with sock.makefile("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        try:
                            self._dispatch(msg)
                        except Exception as e:
                            self.log(f"❌ inet client: {type(e).__name__}: {e}")
            except OSError:
                pass
            finally:
                with self._wlock:
                    self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                self.connected = False
                self.members = {}
                self.state = ""
            if self.joined:
                self.on_event("disconnected", {"reason": "lost the hub"})
                self._kick.wait(2)
                self._kick.clear()
            else:
                self.on_event("left", {})

    def _dispatch(self, msg: dict):
        t = msg.get("t")
        if t == "welcome":
            self.name = msg.get("name", self.name)
            self.network = msg.get("network") or self.network
            self.members = {m["name"]: m for m in msg.get("members") or []}
            self.transcript.clear()
            self.transcript.extend((msg.get("history") or [])[-self.transcript.maxlen:])
            self.connected = True
            self.on_event("welcome", msg)
        elif t == "members":
            self.members = {m["name"]: m for m in msg.get("members") or []}
            self.on_event("members", msg)
        elif t == "status":
            if msg.get("name") in self.members:
                self.members[msg["name"]]["state"] = msg.get("state")
            self.on_event("status", msg)
        elif t in ("join", "leave", "error"):
            self.on_event(t, msg)
        elif t == "msg":
            self.transcript.append(msg)
            if msg.get("from") != self.name:
                self.on_message(msg)


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()
