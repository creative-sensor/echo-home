#!/usr/bin/env python
"""
Intelligence Network hub: the room where AI agents (through their iFace) and humans meet.

One asyncio TCP server per network, newline-delimited JSON, no dependencies. The Neovim driver
(inet.lua: :InetUp, or any client that finds the hub down) starts it on demand; standalone:
    python inet_hub.py --name echo-net --port 7788

Speech
  plain text           broadcast to every member
  text with @name      point-to-point: delivered only to the named members (+ sender, + watchers)
  @all / @here         broadcast that mentions everyone
  A client may also pass "to": [...] explicitly ([] = broadcast) instead of relying on mentions.

client -> hub
  {"t":"hello","name":..,"kind":"agent"|"human","purpose":..,"watch":bool}   first line, required
  {"t":"say","text":..,"re":<msg id>|null,"to":[names]?}
  {"t":"status","state":..}       presence shown in rosters (listening / deliberating / thinking ...)
  {"t":"profile","purpose":..}
  {"t":"who"} | {"t":"bye"}

hub -> client
  {"t":"welcome","network":<network name>,"name":<assigned>,"members":[..],"history":[msg..]}
  {"t":"msg","id","ts","from","kind","text","to":[..],"mentions":[..],"re","depth"}
  {"t":"join"|"leave","member":{..}} | {"t":"members","members":[..]} | {"t":"status","name","state"}
  {"t":"error","text"}

"depth" counts agent-to-agent reply hops (a human message resets it); iFaces use it to stop loops.
"watch" members (the human Network tab) also see point-to-point traffic between others.
"""
import argparse
import asyncio
import json
import re
import sys
import time
from collections import deque
from typing import Dict, List, Optional

MENTION = re.compile(r"(?<![\w@])@([\w.-]+)")
EVERYONE = {"all", "here", "everyone"}


def _log(text: str):
    print(f"{time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


class Member:
    def __init__(self, name: str, kind: str, purpose: str, watch: bool, writer: asyncio.StreamWriter):
        self.name, self.kind, self.purpose, self.watch, self.writer = name, kind, purpose, watch, writer
        self.state = "listening" if kind == "agent" else "here"
        self.since = time.time()

    def info(self) -> dict:
        return {"name": self.name, "kind": self.kind, "purpose": self.purpose, "state": self.state,
                "since": self.since}


class Hub:
    def __init__(self, network: str = "inet", history: int = 500):
        self.network = network
        self.members: Dict[str, Member] = {}
        self.history: deque = deque(maxlen=history)
        self.depth: Dict[int, int] = {}
        self.next_id = 1

    # ----- helpers -----
    def canon(self, name: str) -> Optional[str]:
        low = name.lower().rstrip(".-")
        for n in self.members:
            if n.lower() == low:
                return n
        return None

    def unique(self, name: str) -> str:
        name = re.sub(r"[^\w.-]", "_", (name or "").strip())[:32].strip(".-") or "anon"
        if name.lower() in EVERYONE:
            name += "_"
        base, n = name, 2
        while self.canon(name):
            name, n = f"{base}{n}", n + 1
        return name

    def roster(self) -> List[dict]:
        return [m.info() for m in self.members.values()]

    @staticmethod
    def send(m: Member, obj: dict):
        try:
            m.writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass

    def emit(self, obj: dict, only=None):
        for m in list(self.members.values()):
            if only is None or only(m):
                self.send(m, obj)

    @staticmethod
    def visible(msg: dict, m: Member) -> bool:
        return not msg["to"] or m.name == msg["from"] or m.name in msg["to"] or m.watch

    # ----- speech -----
    def say(self, sender: Member, text: str, re_id, to_explicit):
        words = MENTION.findall(text)
        everyone = any(w.lower().rstrip(".-") in EVERYONE for w in words)
        mentions: List[str] = []
        for w in words:
            c = self.canon(w)
            if c and c != sender.name and c not in mentions:
                mentions.append(c)
        if everyone:
            mentions = [n for n in self.members if n != sender.name]

        if isinstance(to_explicit, list):
            to = [c for c in (self.canon(str(n)) for n in to_explicit) if c and c != sender.name]
            if to_explicit and not to:
                self.send(sender, {"t": "error", "text": f"nobody to deliver to: {to_explicit} left the network"})
                return
        else:
            to = [] if everyone else list(mentions)

        try:
            re_id = int(re_id) if re_id is not None else None
        except (TypeError, ValueError):
            re_id = None
        depth = self.depth.get(re_id, -1) + 1 if sender.kind == "agent" and re_id in self.depth else 0

        msg = {"t": "msg", "id": self.next_id, "ts": time.time(), "from": sender.name, "kind": sender.kind,
               "text": text, "to": to, "mentions": mentions, "re": re_id, "depth": depth}
        self.next_id += 1
        self.history.append(msg)
        self.depth[msg["id"]] = depth
        if len(self.depth) > 4000:
            for k in [k for k in self.depth if k < msg["id"] - 2000]:
                del self.depth[k]
        self.emit(msg, only=lambda m: self.visible(msg, m))
        arrow = f" → {', '.join(to)}" if to else ""
        _log(f"#{msg['id']} {sender.name}{arrow} (d{depth}): {text[:200]!r}")

    # ----- connection -----
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        member: Optional[Member] = None
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            hello = json.loads(line or b"{}")
            if hello.get("t") != "hello":
                return
            kind = "agent" if hello.get("kind") == "agent" else "human"
            name = self.unique(str(hello.get("name") or "anon"))
            member = Member(name, kind, str(hello.get("purpose") or "")[:300], bool(hello.get("watch")), writer)
            self.members[name] = member
            history = [h for h in self.history if self.visible(h, member)][-200:]
            self.send(member, {"t": "welcome", "network": self.network, "name": name, "members": self.roster(), "history": history})
            self.emit({"t": "join", "member": member.info()}, only=lambda m: m is not member)
            self.emit({"t": "members", "members": self.roster()})
            _log(f"+ {name} ({kind}) {member.purpose[:80]!r}")
            await writer.drain()

            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                t = m.get("t")
                if t == "say":
                    text = str(m.get("text") or "").strip()
                    if text:
                        self.say(member, text[:20000], m.get("re"), m.get("to"))
                elif t == "status":
                    member.state = str(m.get("state") or "")[:40]
                    self.emit({"t": "status", "name": member.name, "state": member.state})
                elif t == "profile":
                    member.purpose = str(m.get("purpose") or "")[:300]
                    self.emit({"t": "members", "members": self.roster()})
                elif t == "who":
                    self.send(member, {"t": "members", "members": self.roster()})
                elif t == "bye":
                    break
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            if member and self.members.get(member.name) is member:
                del self.members[member.name]
                self.emit({"t": "leave", "member": member.info()})
                self.emit({"t": "members", "members": self.roster()})
                _log(f"- {member.name}")
            try:
                writer.close()
            except Exception:
                pass


async def serve(network: str, host: str, port: int, history: int):
    hub = Hub(network, history)
    server = await asyncio.start_server(hub.handle, host, port, limit=1 << 20)
    _log(f"Intelligence Network '{network}' listening on {host}:{port}")
    async with server:
        await server.serve_forever()


def main():
    p = argparse.ArgumentParser(description="Intelligence Network hub for AI agents and humans")
    p.add_argument("--name", default="inet", help="network name shown to every member")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7788)
    p.add_argument("--history", type=int, default=500, help="messages kept for late joiners")
    a = p.parse_args()
    try:
        asyncio.run(serve(a.name, a.host, a.port, a.history))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        _log(f"cannot listen on {a.host}:{a.port}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
