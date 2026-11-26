#!/usr/bin/env python
"""
ptrm: the port-remote agent. Puts services that run on remote, high-performance hosts at ports of this machine and
keeps them there: it manages the rows of the user's ptrm.md (localhost:<LOCAL_PORT> -> <REMOTE_IP>:<REMOTE_PORT> through
<SSH_HOST>), opens and heals the ssh tunnels, watches what answers, discovers what runs on the hosts, launches a service
there when it is missing (from a remembered recipe), troubleshoots, and remembers every port ever opened and every host
ever accessed (~/echo-home/memory/ptrm). Hosts are not 24/7 servers: when a row that was up fails, ptrm opens an
incident, diagnoses the layer that broke (ptrm_inv), fixes what it can (tunnel, relaunch from the recipe) and asks the
user for "remote hands" (power, wake, cables, network, login …) with a checklist built from the diagnosis, the host's
profile and its runbook; it rechecks until the service is back and keeps the case. Headless backend of ptrm.lua (same
folder); the engine is ptrm_core.py, the host inventory/profiles/diagnosis ptrm_inv.py.

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"w","b":"chat"|"iface"|"log","text":..,"hl":<highlight group>|null}   append lines
  {"t":"w","b":"log","text":..,"raw":true} / {"t":"w","b":"log","text":"","eos":true}   streamed LLM text / its end
  {"t":"state","busy":b,"phase":..,"queue":n,"auto":b,"approval":<text>|null,"net":{..},"model":..,
   "counts":{"up":n,..},"table":<path>,"interval":s,"llm":"host:port"}
  {"t":"dash","lines":[..],"rows":{"<line, 1-based>":{"port":n,"host":h}},"hls":[[line0, col_s, col_e, group], ..]}
  {"t":"stats","key":..,"host":h|null,"tool":..,"cmd":[..],"title":..}        open a monitor terminal in STATS
  {"t":"req","id":N,"op":"context"|"ex"|"lua"|"reload", ...}                    run in the user's editor (id 0: no answer)
  {"t":"show"}   {"t":"exit"}   {"t":"alert","text":..}   (a notification: remote hands needed, incident …)
nvim -> backend
  {"t":"input","text":..,"ctx":{buffer,line,port}|null}
  {"t":"op","op":"check"|"open"|"close"|"reopen"|"history"|"discover"|"reach"|"stats"|"refresh"|"diagnose"|"incident",
   "port":n,"host":h,"tool":t,"incident":id}
  {"t":"res","id":N,"ok":b,"out":..,"err":..}
  {"t":"net","op":"join"|"leave"|"say", ...}
  {"t":"cancel"} | {"t":"quit"}
"""
import argparse
import json
import os
import queue
import re
import shlex
import shutil
import socket
import sys
import threading
import time
from collections import deque
from typing import Optional

_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
for _stream in (sys.stdin, sys.stderr):  # the protocol is UTF-8 whatever the Windows code page is
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import ptrm_core as core  # noqa: E402
import ptrm_inv as pinv  # noqa: E402
from ptrm_core import (ICON, Entry, Memory, Table, Tunnels, clip, utc_now, when)  # noqa: E402

ARGS = None

# ---------------------------------------------------------------- protocol
_wlock = threading.Lock()


def emit(obj: dict):
    with _wlock:
        try:
            _PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
            _PROTO.flush()
        except (OSError, ValueError):
            pass


def chat(text: str = "", hl: Optional[str] = None):
    emit({"t": "w", "b": "chat", "text": text, "hl": hl})


def iface_log(text: str = "", hl: Optional[str] = None):
    emit({"t": "w", "b": "iface", "text": text, "hl": hl})


def logw(text: str = "", hl: Optional[str] = None):
    """A line in the LLM pane (the raw LLM output)."""
    emit({"t": "w", "b": "log", "text": text, "hl": hl})


def parse_llm_json(raw: str) -> Optional[dict]:
    """First balanced JSON object in the text (tolerates fences, prose, <think> blocks)."""
    if not raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    for start in [m.start() for m in re.finditer(r"\{", raw)]:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
    return None


# ---------------------------------------------------------------- LLM
THINK_HL = "PtrmThink"  # the model's thinking in the LLM pane (dim italic; :hi PtrmThink to change)


class LLM:
    def __init__(self, host, port, think=True):
        from openai import OpenAI
        self.base = f"http://{host}:{port}/v1"
        self.port = int(port)
        self.client = OpenAI(base_url=self.base, api_key="localm", timeout=600)
        self.model = None
        self.think = think

    def ensure(self) -> bool:
        if self.model:
            return True
        try:
            models = self.client.models.list()
            self.model = models.data[0].id if models.data else None
        except Exception:
            self.model = None
        return bool(self.model)

    def _kw(self):  # gemma-4's template thinks only when asked (enable_thinking); the thinking is shown in the LLM pane
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": bool(self.think)}}}

    def stream(self, messages, cancel: threading.Event) -> str:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base} (start it, then ask again)")
        try:
            resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.0,
                                                       stream=True, **self._kw())
        except Exception:
            self.model = None  # the server changed (restarted, other model): look again next time
            raise
        out, buf, last, mode, tag = "", "", time.time(), "text", False

        def flush():
            nonlocal buf, last
            if buf:
                emit({"t": "w", "b": "log", "text": buf, "raw": True, "hl": THINK_HL if mode == "think" else None})
                buf, last = "", time.time()

        def put(text, m):  # thinking and the answer go out as separate pieces: the thinking in its own colour
            nonlocal buf, mode
            if not text:
                return
            if m != mode:
                if mode == "think":
                    buf += "\n"
                flush()
                mode = m
                if m == "think":
                    buf = "💭 "
            buf += text
        try:
            for chunk in resp:
                if cancel.is_set():
                    break
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                put(getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None) or "", "think")
                txt = d.content or ""
                while txt:  # a server that leaves <think>…</think> in the content
                    if tag:
                        i = txt.find("</think>")
                        put(txt if i < 0 else txt[:i], "think")
                        txt, tag = ("", True) if i < 0 else (txt[i + 8:], False)
                    else:
                        i = txt.find("<think>")
                        piece = txt if i < 0 else txt[:i]
                        out += piece
                        put(piece, "text")
                        txt, tag = ("", False) if i < 0 else (txt[i + 7:], True)
                if "\n" in buf or len(buf) > 160 or time.time() - last > 0.1:
                    flush()
        finally:
            if mode == "think":
                buf += "\n"
            flush()
            if cancel.is_set():
                emit({"t": "w", "b": "log", "text": " ✖ cancelled", "raw": True})
            emit({"t": "w", "b": "log", "text": "", "eos": True})
            if cancel.is_set():
                try:
                    resp.close()
                except Exception:
                    pass
        return out

    def json_call(self, system: str, user: str) -> Optional[dict]:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}], **self._kw())
        content = r.choices[0].message.content or ""
        think = getattr(r.choices[0].message, "reasoning_content", None) or ""
        logw("── 🌐 network attention ──", "Comment")
        if think.strip():
            logw("💭 " + think.strip()[-3000:], THINK_HL)
        logw((content.strip()[-1500:] or "(empty)") + "\n", "Comment")
        return parse_llm_json(content)


# ---------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You are ptrm (port-remote), the agent that puts services running on remote high-performance hosts at ports of the user's machine and keeps them there. You work in the user's Neovim and, when joined, on the Intelligence Network.

HOW IT WORKS
- Each row of the user's ptrm.md table maps localhost:<port> to <remote_ip>:<remote_port> as seen from <host> (an ssh destination like user@machine.local). NOTES says what is expected there (kind/name: llm/gemma-4 e2b q8 …): ptrm recalls from it the matching recipe or the procedure that brought it up before. llm/ rows are checked as LLM servers (health, model; ≠ = not the model NOTES asks for). Empty cells: SSH_HOST → ptrm chooses a host from its inventory (shown ~host); REMOTE_IP → 127.0.0.1; REMOTE_PORT → LOCAL_PORT. At start ptrm brings every row up, except disabled rows (LOCAL_PORT written ~~8090~~) and rows on a disabled host ([HOSTS]: DISABLED): ptrm leaves those alone — no bring-up, healing, incident or teach — and never chooses a disabled host, unless the user asks.
- A row is served by an ssh -L tunnel. Row states in [TABLE]: up (answers) · loading (starting) · down (the tunnel is up but nothing answers behind it: the service is down on the host) · stalled · error · lost (ptrm keeps it open but its ssh died) · off (not open) · busy (another local process holds the port) · local (a local process serves it) · other (a tunnel to another host/port) · disabled (the user disabled the row).
- ssh runs non-interactively (keys only). A password-only host cannot be used by ptrm: say so (ssh-copy-id, or ptrmo opens it in a terminal).
- [HOSTS] is what ptrm remembers of each host: inventory (OS + last boot, machine, CPU, RAM, GPUs + VRAM, network: medium/name/IP/gateway, disks, power/sleep, sshd), its profile (pN: one hardware·network·OS configuration; a new one when it moved or changed), recent changes, risks, runbook (physical steps that brought it back before), incidents, services, launch recipes, notes. [RECALL] is past memory matching the request, with past cases (symptom → cause → fix). Use them before going to the hosts.
- The hosts are not 24/7 servers: they sleep, reboot, move networks, lose cables, get GPUs unplugged. When a row that was up fails, ptrm opens an INCIDENT by itself: it diagnoses the layer, fixes what it can (reopens the tunnel, relaunches the service from its recipe) and otherwise asks the user for remote hands with a checklist; it rechecks until the service is back and keeps the case. [INCIDENTS] lists the open ones.

RULES
1. Each turn run ONE action, then read its result. Base every claim on action results and the context, never on guesses.
2. A service wanted at localhost:<port>: if the row is up, say so (with the model for llm rows). Otherwise bringup does the whole procedure (host choice, open, launch what NOTES recalls, else teach); or step by step: check → open → if down: launch from the recipe → check again.
13. When memory does not say how to start a service (NOTES recalls nothing, NOTES unclear, never started on that host), use teach: a terminal opens on the host where the user brings it up; ptrm learns the recipe from it. Do not guess command lines for unknown services.
3. No row for what the user wants: recall/[HOSTS] to find which host serves or served it (recipes, services) and set_entry a row (pick a free local port from local_ports when none is asked), then open it.
4. Troubleshooting: diagnose first (it finds the layer that broke — this machine's network, name resolution, host down/asleep, firewall, sshd, key, host key, service, tunnel — with evidence compared to the host's profile), then check/discover/remote. Name the cause plainly. When a host is gone, propose another host that has the service (recipes, services in [HOSTS]); change the table only when the user asks.
5. Never kill local processes you did not start; a port held by another local process: report who holds it and propose a free port.
6. remote, local, launch, stop, docker changes, closing tunnels opened elsewhere and editor actions ask the user's approval; prefer read-only steps first. remote is for short commands that return: never start a server with remote, use launch (it detaches it and waits until it answers).
7. A service ptrm launches is reached through the tunnel: bind it to 127.0.0.1 (--host 127.0.0.1, --bind 127.0.0.1) unless the user asks otherwise. Prefer the host's recipe; on Windows hosts commands run as Windows command lines.
8. Keep what you learn: note the cause and the fix on the port or the host (note), so the next troubleshooting is faster.
9. Finish with declare_result: "reply" is the concrete answer (ports, hosts, states, models, what you changed, what remains for the user).
10. Output ONLY one JSON object with only the fields the action needs.
11. What only a human can do (power, wake, cables, Wi-Fi, log in after a reboot, firewall/sshd settings on the host, plug a GPU): ask with hands — concrete steps, one per line, from the diagnosis, the host's runbook and risks. Never claim a physical step was done.
12. When the user says what they did for an incident, pass it with incident (id, human); keep a physical step that mattered with runbook.

ACTIONS — "action_type" is exactly one of the names below; all other fields at the top level of the same object.
Seeing:
  {"action_type":"status"}                          the table, live (also in [TABLE])
  {"action_type":"check","port":8081}               deep check: who listens, tunnel, probe (HTTP, model), ssh log, host reachable, service health on the host
  {"action_type":"local_ports","from":8081,"to":8999}   what listens here in that range, and free ports
Tunnels:
  {"action_type":"open","port":8081}                open the row's tunnel
  {"action_type":"close","port":8081}
  {"action_type":"reopen","port":8081}              close + open (after the row or the remote side changed)
Table (the user's ptrm.md):
  {"action_type":"set_entry","port":8091,"host":"user@h","remote_port":8080,"remote_ip":"127.0.0.1","notes":"llm/name"}   add a row, or change some fields of a row
  {"action_type":"remove_entry","port":8091}
  {"action_type":"disable","port":8091}             ptrm leaves the row alone (no bring-up, healing, incident); "enable" undoes it
  {"action_type":"disable_host","host":"user@h"}    ptrm stops using the host (its rows left alone, never chosen); "enable_host" undoes it
Hosts (ssh):
  {"action_type":"reach","host":"user@h"}           reachable? latency, os, or why not
  {"action_type":"diagnose","port":8081}            or "host" (+ "remote_port"): the layer that broke, evidence, what ptrm can fix, the steps for a human
  {"action_type":"discover","host":"user@h"}        services + processes, docker, recipes, and the inventory: profile, what changed, risks (remembered)
  {"action_type":"remote","host":"user@h","command":"..."}   a shell command there (sh; Windows hosts run MSYS bash)
  {"action_type":"launch","host":"user@h","port":8080,"command":"...","service":"llm/name","health":"/health","wait":180}   start a service there, detached, and wait until it answers; without "command": the recipe NOTES/port recall
  {"action_type":"bringup","port":8080}            the row's service up: choose the host (SSH_HOST empty), open, launch what NOTES recalls, else teach
  {"action_type":"teach","port":8080,"why":".."}   open a terminal on the row's host (STATS) for the user to start the service; ptrm records it and learns the recipe
  {"action_type":"stop","host":"user@h","port":8080}   stop what listens on that remote port (or "pid")
  {"action_type":"logs","host":"user@h","port":8080}   the output of a service ptrm launched there
  {"action_type":"docker","host":"user@h","args":"ps"}   docker CLI against the host's engine (over ssh)
Remote hands (the user has physical access to the hosts):
  {"action_type":"hands","host":"user@h","steps":["..",".."],"why":".."}   ask the user to do these steps at the host (joins its incident)
  {"action_type":"incident","id":3,"human":"what the user did"}   or "close":"why" (dismiss it)
  {"action_type":"runbook","host":"user@h","step":".."}   keep a physical step that matters for this host
Memory:
  {"action_type":"recall","query":"gemma 26B"}       search everything ptrm remembers (also past cases)
  {"action_type":"history","port":8081}             or "host": the history of a port or a host
  {"action_type":"note","port":8081,"text":".."}    or "host": keep a fact (cause, fix, quirk)
Screen and editor:
  {"action_type":"stats","host":"user@h","tool":"htop"}   a live monitor terminal in the STATS area (no host = this machine; tool optional: top htop btop nvtop macmon gpu)
  {"action_type":"local","command":"..."}           a shell command on this machine
  {"action_type":"run_command","command":"<Ex command>"}   {"action_type":"run_lua","code":"<Lua>"}
Network:
  {"action_type":"who"}  {"action_type":"ask_agent","to":"memphia","text":"..","wait":120}  {"action_type":"wait_agent","from":"memphia","wait":120}  {"action_type":"post","text":"..","to":["name"]}
Finish:
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
Every action also takes "reason": one short sentence why.
"""

ACTION_NAMES = ["status", "check", "local_ports", "open", "close", "reopen", "set_entry", "remove_entry", "reach",
                "diagnose", "discover", "remote", "launch", "stop", "logs", "docker", "hands", "incident", "runbook",
                "bringup", "teach", "disable", "enable", "disable_host", "enable_host",
                "recall", "history", "note", "stats", "local", "run_command", "run_lua", "who", "ask_agent",
                "wait_agent", "post", "declare_result"]
ACTION_ALIASES = {"open_tunnel": "open", "close_tunnel": "close", "restart": "reopen", "forward": "open",
                  "probe": "check", "check_port": "check", "ping": "reach", "ssh": "remote", "run_remote": "remote",
                  "exec": "remote", "shell": "local", "search": "recall", "memory": "recall", "add_entry": "set_entry",
                  "update_entry": "set_entry", "edit_entry": "set_entry", "start": "launch", "provision": "launch",
                  "kill": "stop", "log": "logs", "monitor": "stats", "top": "stats", "free_ports": "local_ports",
                  "table": "status", "list": "status", "result": "declare_result", "finish": "declare_result",
                  "answer": "declare_result", "inventory": "discover", "troubleshoot": "diagnose", "diagnosis": "diagnose",
                  "remote_hands": "hands", "ask_human": "hands", "human": "hands", "ask_user": "hands",
                  "bring_up": "bringup", "ensure": "bringup", "learn": "teach", "show_me": "teach",
                  "disable_entry": "disable", "enable_entry": "enable", "ignore": "disable"}
HOST_LAYERS = ("local-net", "dns", "host-down", "ssh-blocked", "sshd-down", "auth", "hostkey")
INC_BAD = ("down", "lost", "stalled", "error")
ACK = re.compile(r"(?i)\s*(ok|okay|done|yes|y|fixed|thanks?|thank you|it'?s (up|back|on)|try( it)? again|check( it)? again|go)[.!]*\s*")
PHYSICAL = re.compile(r"(?i)\b(plug|unplug|cable|power|turn(ed)? on|switch(ed)? on|woke|wake|press|button|lid|log(ged)? ?in|"
                      r"reboot|restart|connect|wi-?fi|ethernet|router|switch|egpu|gpu|dock|charger|battery|fan|monitor|"
                      r"keyboard|mouse|firewall|sshd|service|i did|i have|i've|done)\b")
# what the user typed reads as a report of what they did at a host (attached to the incident waiting for hands)
REPORT = re.compile(r"(?i)^\s*(done|ok|okay|fixed|(it'?s|it is) (up|on|back)|back)\b|\b(i|we)\b(?:'ve| have| just)?\s+(?:\w+\s+){0,2}?"
                    r"(did|pressed|plugged|unplugged|replugged|turned|switched|woke|restarted|rebooted|connected|"
                    r"reconnected|logged|opened|closed|replaced|moved|started|fixed|checked|powered|reseated|attached)\b")
MONITORS = {"top", "htop", "btop", "nvtop", "macmon", "nvidia-smi", "gpu", "glances", "atop", "intel_gpu_top"}
DOCKER_READONLY = {"ps", "images", "logs", "inspect", "stats", "version", "info", "top", "port", "events"}


def normalize_action(act: dict) -> dict:
    kind = str(act.get("action_type") or act.get("action") or "").strip()
    kind = ACTION_ALIASES.get(kind, kind)
    if kind in ACTION_NAMES:
        return dict(act, action_type=kind)
    for sep in (":", ".", "/", " "):
        tail = kind.rsplit(sep, 1)[-1].strip()
        tail = ACTION_ALIASES.get(tail, tail)
        if tail in ACTION_NAMES:
            return dict(act, action_type=tail)
    for name in ACTION_NAMES:
        if name in act and isinstance(act[name], dict):
            out = {k: v for k, v in act.items() if k not in (name, "action_type", "action")}
            out.update(act[name])
            out["action_type"] = name
            return out
    return act


def as_port(v) -> Optional[int]:
    m = re.search(r"\d{2,5}", str(v if v is not None else ""))
    return int(m.group(0)) if m and 0 < int(m.group(0)) < 65536 else None


# ---------------------------------------------------------------- agent
class Cancelled(Exception):
    pass


class Agent:
    def __init__(self, args):
        self.args = args
        self.llm = LLM(args.host, args.port, args.think)
        self.mem = Memory(args.memory)
        self.table = Table(args.table or core.default_table(), resolve=self.mem.assignment)
        self.tun = Tunnels(self.mem.run_dir, args.ssh)
        self.running = True
        self.tasks: "queue.Queue" = queue.Queue()
        self.ops: "queue.Queue" = queue.Queue()
        self.pending = 0
        self.current = None  # (text, origin)
        self.phase = "idle"
        self.cancel = threading.Event()
        self.auto = args.auto or autopilot_allows()
        self.approval = None
        self.recent: deque = deque(maxlen=6)
        self.reqs = {}
        self.req_id = 0
        self.rlock = threading.Lock()
        # the live picture
        self.rows: list = []
        self.listeners: dict = {}
        self.prev: dict = {}  # port -> state at the last refresh
        self.seen_up: set = set()  # ports recorded as seen open this session
        self.opening: set = set()
        self.heal: dict = {}  # port -> {"tries", "next", "err"}
        self.refreshed = 0.0
        self.slock = threading.Lock()  # one status pass at a time
        self.oplock = threading.RLock()  # tunnel changes, one at a time
        self.wake = threading.Event()
        # incidents: a row that mattered went bad (run/incidents.json keeps the open ones across restarts)
        self.inc_lock = threading.RLock()
        self.incidents: dict = {}
        self.inc_seq = 0
        self.load_incidents()
        self.bad_since: dict = {}  # port -> time first seen bad
        self.ever_up: set = set()  # ports seen up this session
        self.good_state: dict = {}  # port -> its last good state
        self.approve_timeout = None
        self.next_inv = time.time() + 20
        self.inv_running = False
        self.bringing: set = set()  # rows being brought up (no incident for them meanwhile)
        self.teaching: dict = {}  # port -> an open teach terminal (run/teach.json)
        self.load_teaching()
        self.disabled_prev = None  # the disabled rows at the last look (None: not looked yet)
        self.off_hosts: set = set(self.mem.disabled_hosts())  # hosts the user disabled (Xh): ptrm does not use them
        self.stats_hosts = False  # STATS shows a monitor on each host of the table (g:ptrm_stats {'hosts'})
        self.stats_seen: set = set()  # hosts whose monitor was opened that way
        from ptrm_iface import IFace
        self.iface = IFace(self, args, self.llm.json_call, iface_log)

    # ----- state
    @property
    def busy(self):
        return self.current is not None

    def has_net_work(self):
        return bool(self.current and self.current[1]) or self.pending > 0

    def describe_busy(self):
        return (f"busy with: {clip(self.current[0], 160)}" if self.current else "idle") + f"; {self.pending} queued"

    def counts(self) -> dict:
        c: dict = {}
        for r in self.rows:
            c[r["state"]] = c.get(r["state"], 0) + 1
        return c

    def dash_brief(self) -> str:
        c = self.counts()
        return ", ".join(f"{n} {k}" for k, n in c.items()) or "no rows"

    def net_table(self) -> str:
        return "\n".join(core.row_line(r) for r in self.rows[:20]) or "(no rows)"

    def emit_state(self):
        emit({"t": "state", "busy": self.busy, "phase": self.phase, "queue": self.pending, "auto": self.auto,
              "approval": self.approval["text"] if self.approval else None, "net": self.iface.summary(),
              "model": self.llm.model, "counts": self.counts(), "table": self.table.path,
              "interval": self.args.interval, "llm": f"{self.args.host}:{self.args.port}",
              "heal": self.args.heal, "think": bool(self.llm.think)})

    def set_phase(self, p):
        self.phase = p
        self.emit_state()

    # ----- the live picture: watcher, transitions, healing, DASH
    def watcher(self):
        while self.running:
            try:
                self.refresh()
            except Exception as e:
                logw(f"watch error: {type(e).__name__}: {e}", "ErrorMsg")
            self.wake.wait(self.args.interval)
            self.wake.clear()

    def refresh(self):
        with self.slock:
            rows, listeners = core.status(self.table, self.tun, self.args.probe_timeout)
            for r in rows:  # a row on a disabled host is left alone like a disabled row
                if r.get("host") and r["host"] in self.off_hosts and r["state"] != "unlisted":
                    r["host_off"] = True
                    if r["state"] in ("off", "lost"):
                        r["state"] = "disabled"
            self.rows, self.listeners, self.refreshed = rows, listeners, time.time()
            self.transitions(rows)
            self.heal_tick(rows)
            self.detect(rows)
            self.incident_tick(rows)
            self.inventory_tick(rows)
            self.teach_tick()
        self.emit_dash()
        self.emit_state()

    def fresh_rows(self, max_age: float = 3.0):
        if time.time() - self.refreshed > max_age:
            self.refresh()
        return self.rows

    def transitions(self, rows):
        dis = {r["port"] for r in rows if r.get("disabled")}
        first = self.disabled_prev is None
        for port in sorted(dis - (self.disabled_prev or set())):
            self.on_disabled(port, first)
        for port in sorted((self.disabled_prev or set()) - dis):
            if any(r["port"] == port for r in rows):
                chat(f"▶ {port} enabled again: bringing it up", "Title")
                self.mem.event("enable", port=port, detail="the row is enabled again")
                if self.args.bringup and port not in self.bringing:
                    self.enqueue_bringup(port, auto=True)
        self.disabled_prev = dis
        for r in rows:
            port, st = r["port"], r["state"]
            if r.get("adopt") and r.get("host"):
                self.mem.assign(port, r["host"], r["remote_port"], "the tunnel open on it at start")
            old = self.prev.get(port)
            pr = r.get("probe") or {}
            if st in ("up", "loading", "down", "stalled") and r.get("tunnel") and port not in self.seen_up \
                    and not (r["tunnel"].get("managed")) and r["state"] != "unlisted":
                # a tunnel opened elsewhere (a ptrmo terminal …): every port ever open goes to memory
                self.seen_up.add(port)
                self.mem.port_opened(Entry(r["host"], port, r["remote_ip"], r["remote_port"], r.get("notes", "")), True,
                                     how="seen")
            if old is None or old == st:
                continue
            detail = f"{old} → {st}" + (f" · model {pr['model']}" if pr.get("model") else "")
            self.mem.event("state", port=port, host=r["host"], remote=f"{r['remote_ip']}:{r['remote_port']}",
                           service=r.get("service"), ok=st in ("up", "local", "loading"), detail=detail)
            if st == "off" and old in ("off", "lost") or "disabled" in (st, old):
                continue  # disabling / enabling says so itself
            hl = {"up": "DiagnosticOk", "local": "DiagnosticOk", "loading": "Comment", "off": "Comment"}.get(st, "WarningMsg")
            msg = f"{ICON.get(st, '?')} {port} {old} → {st} · {r['host']} {r['remote_ip']}:{r['remote_port']} {r.get('notes') or ''}"
            if st == "down":
                msg += "\n   the tunnel is up but nothing answers on the host: the service is down there" + (
                    "" if self.args.incidents else " (ask me: \"fix " + str(port) + "\")")
            elif st == "lost" and self.args.heal:
                msg += "\n   its ssh died; healing (reopening) it"
            chat(msg.rstrip(), hl)
        self.prev = {r["port"]: r["state"] for r in rows}

    def on_disabled(self, port: int, first: bool = False):
        """The row is disabled (in the table, X in DASH, ===off): ptrm stops keeping it — no healing, no bring-up, no
        incident, no teach. Disabled during the session: the tunnel ptrm keeps on it is closed too."""
        self.tun.set_want(port, False)
        self.heal.pop(port, None)
        self.bad_since.pop(port, None)
        if port in self.teaching:
            self.stop_teach(port, "the row was disabled")
        inc = self.incident_of(port)
        if inc:
            inc["ports"].pop(str(port), None)
            if not inc["ports"]:
                self.dismiss_incident(inc, f"{port} was disabled")
            else:
                self.save_incidents()
        if first:
            chat(f"🚫 {port} is disabled: I leave it alone", "Comment")
            return
        t = self.tun.scan().get(port)
        closed = ""
        if t and t.get("managed"):
            r = self.tun.close(port, pid=t["pid"])
            closed = f"; closed its tunnel (ssh pid {t['pid']})" if r["ok"] else f"; could not close its tunnel: {r.get('err')}"
        chat(f"🚫 {port} disabled: I no longer bring it up, heal it or open incidents for it{closed}", "WarningMsg")
        self.mem.event("disable", port=port, detail="the row is disabled" + closed)
        self.wake.set()

    def set_disabled(self, port: int, on: bool) -> str:
        e = self.table.set_disabled(port, on)
        if not e:
            return f"Error: no row for {port}"
        emit({"t": "req", "id": 0, "op": "reload", "path": self.table.path})
        self.wake.set()
        return (f"{port} disabled (LOCAL_PORT written ~~{port}~~): ptrm leaves it alone" if on
                else f"{port} enabled: ptrm brings it up and keeps it")

    def set_host_disabled(self, host: str, on: bool, why: str = "") -> str:
        """Xh: ptrm stops using the host — no bring-up, healing, incident, inventory or monitor there, never chosen for
        a row whose SSH_HOST is empty. Its tunnels kept by ptrm close; the rows ptrm had put there (SSH_HOST empty)
        move to another host. Enabled again: its rows are brought up."""
        host = (host or "").strip()
        if not host:
            return "Error: no host"
        if bool(on) == (host in self.off_hosts):
            return f"{host} is already {'disabled' if on else 'enabled'}"
        self.mem.set_host_disabled(host, on, why)
        entries = self.table.entries()
        if not on:
            self.off_hosts.discard(host)
            ports = [e.lport for e in entries if not e.disabled and ((e.host == host and not e.auto_host)
                                                                    or (e.auto_host and not e.host))]  # + rows with no host yet
            if self.args.bringup:
                for p in ports:
                    if p not in self.bringing:
                        self.enqueue_bringup(p, auto=True)
            self.wake.set()
            return f"host {host} enabled" + (f": bringing up {', '.join(map(str, ports))}" if ports and self.args.bringup else "")
        self.off_hosts.add(host)
        closed, moved = [], []
        for port, t in sorted(self.tun.scan().items()):
            if t["host"] == host and t.get("managed"):
                self.tun.set_want(port, False)
                self.heal.pop(port, None)
                if self.tun.close(port, pid=t["pid"])["ok"]:
                    closed.append(port)
        for e in entries:
            if e.host != host:
                continue
            self.tun.set_want(e.lport, False)
            self.heal.pop(e.lport, None)
            self.bad_since.pop(e.lport, None)
            if e.lport in self.teaching:
                self.stop_teach(e.lport, f"its host {host} was disabled")
            if e.auto_host:  # ptrm put it there: another host takes it
                self.mem.assign(e.lport, None)
                if not e.disabled and self.args.bringup:
                    moved.append(e.lport)
                    self.enqueue_bringup(e.lport, auto=True)
        inc = self.incident_of(host=host)
        if inc:
            self.dismiss_incident(inc, f"the host {host} was disabled")
        self.wake.set()
        return (f"host {host} disabled: I no longer use it" + (f"; closed {', '.join(map(str, closed))}" if closed else "")
                + (f"; moving {', '.join(map(str, moved))} to another host" if moved else ""))

    def heal_tick(self, rows):
        if not self.args.heal:
            return
        now = time.time()
        for r in rows:
            port = r["port"]
            if r["state"] == "lost" and r.get("want") and port not in self.opening:
                h = self.heal.setdefault(port, {"tries": 0, "next": 0.0, "err": ""})
                if now < h["next"] or h["tries"] >= self.args.heal_tries:
                    continue
                h["tries"] += 1
                h["next"] = now + min(300.0, 10.0 * 2 ** (h["tries"] - 1))
                threading.Thread(target=self._heal_one, args=(port, h), daemon=True).start()
            elif r["state"] in ("up", "loading", "down", "stalled") and port in self.heal:
                self.heal.pop(port, None)

    def _heal_one(self, port, h):
        e = self.table.get(port)
        if not e:
            return
        out = self.open_entry(e, how="heal")
        ok = not out.startswith("Error")
        h["err"] = "" if ok else out
        chat(f"🩹 heal {port} (try {h['tries']}/{self.args.heal_tries}): {clip(out, 300)}", "DiagnosticOk" if ok else "WarningMsg")
        if not ok and h["tries"] >= self.args.heal_tries:
            chat(f"   gave up reopening {port}; ask me to troubleshoot it (\"why is {port} lost?\")", "ErrorMsg")
            self.mem.event("heal", port=port, host=e.host, ok=False, detail=f"gave up after {h['tries']} tries: {clip(out, 200)}")

    def emit_dash(self):
        lines, rows_map, hls = [], {}, []
        cur = {"text": "", "segs": []}

        def seg(text, hl=None):
            if hl:
                s = len(cur["text"].encode("utf-8"))
                cur["segs"].append((s, s + len(text.encode("utf-8")), hl))
            cur["text"] += text

        def nl(meta=None):
            for s, e, hl in cur["segs"]:
                hls.append([len(lines), s, e, hl])
            if meta:
                rows_map[str(len(lines) + 1)] = meta
            raw = cur["text"].encode("utf-8")
            keep = max([e for _, e, _ in cur["segs"]] + [len(cur["text"].rstrip().encode("utf-8"))])
            lines.append(raw[:keep].decode("utf-8", "replace"))  # trailing spaces go, except a chip's own
            cur["text"], cur["segs"] = "", []

        rows = self.rows
        c = self.counts()
        order = ["up", "loading", "local", "down", "stalled", "error", "lost", "busy", "other", "unlisted", "off", "disabled"]
        seg(" " + "  ".join(f"{ICON.get(k, '?')} {c[k]} {k}" for k in order if c.get(k)) or " no rows")
        seg(f"   ⟳ {self.args.interval:g}s · {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}", "Comment")
        nl()
        wh = max([len(r["host"]) + (1 if r.get("auto") else 0) for r in rows] + [14])
        wh = min(wh, 30)
        seg(f" {'PORT':<6}{'STATE':<12}{'HOST':<{wh + 1}}{'REMOTE':<16}{'SERVICE':<34}{'VIA':<14}MS", "Title")
        nl()
        hl_state = {"up": "DiagnosticOk", "local": "DiagnosticOk", "loading": "DiagnosticInfo", "down": "DiagnosticWarn",
                    "stalled": "DiagnosticWarn", "error": "DiagnosticError", "lost": "DiagnosticError",
                    "busy": "DiagnosticError", "other": "DiagnosticWarn", "unlisted": "DiagnosticHint", "off": "Comment",
                    "disabled": "PtrmDisabled"}
        marks = []  # for the table's buffer: which LOCAL_PORT is up, which REMOTE_PORT is not LOCAL_PORT
        for r in rows:
            st = "opening" if r["port"] in self.opening else r["state"]
            pr = r.get("probe") or {}
            t = r.get("tunnel")
            # PORT: dark green bold when up; struck through when the row is disabled
            seg(" ")
            seg(str(r["port"]), "PtrmPortOff" if r.get("disabled") else "PtrmPortUp" if st == "up" else None)
            seg(" " * max(1, 6 - len(str(r["port"]))))
            differs = str(r["remote_port"]) != str(r["port"])
            if st != "unlisted":
                marks.append({"port": r["port"], "up": st == "up", "differs": differs, "disabled": bool(r.get("disabled")),
                              "host_off": bool(r.get("host_off"))})
            seg(f"{ICON.get(st, '?')} {st:<9}", hl_state.get(st, "DiagnosticInfo"))
            if r.get("auto"):
                seg(f"{clip(('~' + r['host']) if r['host'] else '(ptrm chooses)', wh):<{wh + 1}}", "DiagnosticHint")
            elif r.get("host_off"):  # its host is disabled (Xh): struck through
                seg(clip(r["host"], wh), "PtrmPortOff")
                seg(" " * (wh + 1 - len(clip(r["host"], wh))))
            else:
                seg(f"{clip(r['host'], wh):<{wh + 1}}", "Comment" if st in ("off", "disabled") else None)
            # REMOTE: the port in dark orange bold when it is not the local port
            seg(f"{r['remote_ip']}:", "Comment" if st in ("off", "disabled") else None)
            seg(str(r["remote_port"]), "PtrmPortDiff" if differs else ("Comment" if st in ("off", "disabled") else None))
            seg(" " * max(1, 15 - len(r["remote_ip"]) - len(str(r["remote_port"]))))
            svc = r.get("notes") or ("(not in the table)" if st == "unlisted" else "")
            mm = ""
            if pr.get("model") and pr["model"] not in svc:
                e_ = Entry(r["host"], r["port"], notes=r.get("notes") or "")
                mm = self.model_mismatch(e_, pr["model"])
                svc = f"{r.get('notes') or svc} {'≠' if mm else '▸'} {pr['model']}"
            seg(f"{clip(svc, 33):<34}", "Comment" if st in ("off", "disabled") else "WarningMsg" if mm else "String")
            via = ""
            if r["port"] in self.teaching:
                via = "(teaching)"
            elif r["port"] in self.bringing:
                via = "(bringing up)"
            elif t:
                via = f"ssh {t['pid']}" + ("" if t.get("managed") else "*")
            elif r.get("holders"):
                via = clip(r["holders"][0]["name"], 13)
            elif r.get("want"):
                via = "(kept)"
            if (r.get("disabled") or r.get("host_off")) and st != "disabled":
                via = (via + " " if via else "") + "🚫"
            seg(f"{via:<14}", "Comment")
            if pr.get("ms") is not None and st in ("up", "loading", "local"):
                seg(f"{pr['ms']}")
            nl({"port": r["port"], "host": r["host"]})
        seg(" * opened elsewhere · ~host ptrm's choice · ≠ not the NOTES model · ", "Comment")
        seg("8080", "PtrmPortUp")
        seg(" up · :", "Comment")
        seg("8090", "PtrmPortDiff")
        seg(" ≠ PORT · ", "Comment")
        seg("8080", "PtrmPortOff")
        seg(" disabled (X)", "Comment")
        nl()
        if self.incidents:
            nl()
            seg(" INCIDENTS", "Title")
            seg("   <CR> the details · D diagnose again · ===d <what you did> · ===i x <id> close", "Comment")
            nl()
            for inc in list(self.incidents.values()):
                dg = inc.get("diag") or {}
                ports = " ".join(f"{p} {v['state']}" for p, v in inc["ports"].items())
                seg(f" #{inc['id']:<3}", "DiagnosticError")
                seg(f"{clip(inc['host'], wh)} ", "PtrmInvNameBad")
                seg(f" {ports} · since {when(inc['opened'])[11:]} ({core.ago(inc['opened'])}) · {dg.get('layer') or 'diagnosing'}")
                st = {"hands": "  🖐 waiting for you", "watching": "  👀 watching", "diagnosing": "  🔎 diagnosing"}.get(inc["state"], "")
                seg(st, "WarningMsg" if inc["state"] == "hands" else "Comment")
                meta = {"incident": inc["id"], "host": inc["host"], "port": int(next(iter(inc["ports"]))) if inc["ports"] else None}
                nl(meta)
                if dg.get("cause"):
                    seg(f"      {clip(dg['cause'], 150)}", "Comment")
                    nl(meta)
        nl()
        seg(" HOSTS", "Title")
        seg("   ✔ last reached · OS · CPU cores · MEM GB · GPU VRAM GB · NET medium, name, IP · SVC services (besides ssh) · Xh disable", "Comment")
        nl()
        hosts = []
        for r in rows:  # the hosts of ptrm.md (named, or chosen by ptrm) …
            if r["host"] and r["state"] != "unlisted" and r["host"] not in hosts:
                hosts.append(r["host"])
        for h in self.mem.known_hosts():  # … and every host with an inventory (this client's or another's)
            if h not in hosts and self.mem.host(h).get("inventory"):
                hosts.append(h)
        # Chips: ' OS ' then ' win11 ' — one space each side of the value inside its colour; the columns are lined up
        # with plain spaces after a chip (or a group of chips: NET), never inside it.
        NW = {"type": 4, "name": 12, "ip": 15}
        NET_HL = {"type": "PtrmNetType", "name": "PtrmNetName", "ip": "PtrmNetIP"}
        info = []
        for h in hosts[:16]:
            d = self.mem.host(h)
            inv = d.get("inventory") or {}
            cs = pinv.chips(inv, d) if (inv or (d.get("os") or {}).get("system")) else []
            nc = [(w, clip(v, NW[w])) for w, v in pinv.net_chips(inv)]
            svc = sum(1 for x in d.get("services") or [] if x.get("service") != "ssh" and x.get("port") != 22) \
                if (d.get("services") is not None or inv) else None
            info.append((h, d, inv, cs, nc, svc))
        labels = [lb for lb, _ in next((x[3] for x in info if x[3]), [])]
        cw = {lb: max([len(v) for x in info for l2, v in x[3] if l2 == lb] + [1]) for lb in labels}
        netw = max([sum(len(v) + 2 for _, v in x[4]) for x in info] + [0])
        hw = max([len(clip(x[0], wh)) for x in info] + [1])

        def chip(text, hl, width=0):
            seg(f" {text} ", hl)
            if width > len(text):
                seg(" " * (width - len(text)))

        for h, d, inv, cs, nc, nsvc in info:
            err = d.get("last_error") or {}
            ok = bool(d.get("last_ok") and (not err or err.get("at", "") <= d["last_ok"]))
            seg(" ")
            chip(clip(h, wh), "PtrmInvNameOff" if h in self.off_hosts else "PtrmInvName" if ok or not err else "PtrmInvNameBad", hw)
            if ok:
                seg(f" ✔ {when(d['last_ok'])} ", "DiagnosticOk")
            elif err:
                seg(f" ✖ {when(err.get('at'))} ", "DiagnosticError")
            else:
                seg(f" {'? not reached yet':<19} ", "Comment")
            vals = dict(cs)
            for label in labels:
                if label in vals:
                    seg(" ")
                    seg(f" {label} ", "PtrmInvLabel")
                    chip(vals[label], "PtrmInvVal", cw[label])
                else:
                    seg(" " * (1 + len(label) + 2 + cw[label] + 2))
            if netw:  # ' NET ' then medium · network name · IP, each in its own colour, side by side
                if nc:
                    seg(" ")
                    seg(" NET ", "PtrmInvLabel")
                    for what, val in nc:
                        seg(f" {val} ", NET_HL[what])
                    seg(" " * (netw - sum(len(v) + 2 for _, v in nc)))
                else:
                    seg(" " * (1 + 5 + netw))
            if nsvc is not None:
                seg(" ")
                seg(" SVC ", "PtrmInvLabel")
                seg(f" {nsvc} ", "PtrmSvcVal")
            extra = []
            if d.get("profile") and len(d.get("profiles") or []) > 1:
                extra.append(f"{d['profile']}/{len(d['profiles'])}")
            if extra:
                seg("  " + " · ".join(extra), "Comment")
            nrisk = sum(1 for x in d.get("risks") or [] if x.get("sev"))
            if nrisk:
                seg(f" ⚠{nrisk}", "DiagnosticWarn")
            if h in self.off_hosts:
                seg(" 🚫 disabled (Xh enables)", "PtrmDisabled")
            inc = self.incident_of(host=h)
            if inc:
                seg(f" #{inc['id']}", "DiagnosticError")
            nl({"host": h})
            ch = (d.get("changes") or [])[-1:]
            if ch and time.time() - core.epoch(ch[0].get("at")) < 86400:
                items = ch[0].get("items") or []
                seg(f"   ↳ changed {when(ch[0]['at'])[11:]}: " + clip("; ".join(x["what"] for x in items), 150),
                    "DiagnosticWarn" if any(x.get("sev") for x in items) else "Comment")
                nl({"host": h})
        nl()
        seg(" RECENT", "Title")
        seg(f"   memory: {len(self.mem.known_hosts())} hosts · {len(self.mem.known_ports())} ports · {self.mem.root}", "Comment")
        nl()
        ev = [e for e in self.mem.events(limit=400) if e.get("kind") != "note"][-8:]
        for e in reversed(ev):
            ok = e.get("ok")
            seg(f" {e.get('ts', '')[5:10]} {e.get('ts', '')[11:16]}Z ", "Comment")
            kind = {"host_change": "changed"}.get(e.get("kind", ""), e.get("kind", ""))
            seg(f"{kind:<9}", "DiagnosticOk" if ok is True else "DiagnosticError" if ok is False
                else "DiagnosticWarn" if kind in ("changed", "hands") else None)
            seg(clip(" ".join(str(x) for x in (e.get("port", ""), e.get("host", ""), e.get("detail", "")) if x != ""), 110))
            nl({"port": e["port"]} if e.get("port") else ({"host": e["host"]} if e.get("host") else None))
        nl()
        seg(" <CR> check · o open · x close · R reopen · B bring up · T teach · D diagnose · X disable (Xh: the host) · s stats · t terminal · d discover · h history · e table", "Comment")
        nl()
        emit({"t": "dash", "lines": lines, "rows": rows_map, "hls": hls, "marks": marks, "table": self.table.path})

    # ----- incidents: a row that mattered went bad → diagnose → fix what ptrm can → remote hands → recheck → learn
    def _inc_path(self) -> str:
        return os.path.join(self.mem.run_dir, "incidents.json")

    def load_incidents(self):
        try:
            with open(self._inc_path(), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.inc_seq = int(d.get("seq") or 0)
        self.incidents = {int(i["id"]): i for i in d.get("open") or [] if i.get("id")}
        for i in self.incidents.values():
            i.update(queued=False, probing=False, busy=False, next_check=0)

    def save_incidents(self):
        with self.inc_lock:
            data = {"seq": self.inc_seq, "open": [dict(i, queued=False, probing=False, busy=False) for i in self.incidents.values()]}
            tmp = self._inc_path() + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self._inc_path())
            except OSError as e:
                logw(f"incidents not saved: {e}", "ErrorMsg")

    def incident_of(self, port=None, host=None) -> Optional[dict]:
        for i in list(self.incidents.values()):
            if port is not None and str(port) in i["ports"]:
                return i
            if port is None and host and i["host"] == host:
                return i
        return None

    def detect(self, rows):
        """Open an incident for a row that matters (ptrm keeps it open, or it was up this session) once it stays bad:
        down/stalled/error for --incident-grace seconds, lost once healing failed (or healing is off)."""
        now = time.time()
        for r in rows:
            port, st = r["port"], r["state"]
            if st in ("up", "loading", "local"):
                self.ever_up.add(port)
                self.good_state[port] = st
            if not self.args.incidents or st == "unlisted" or st not in INC_BAD or not (r.get("want") or port in self.ever_up) \
                    or port in self.bringing or port in self.teaching or r.get("disabled") or r.get("host_off"):
                self.bad_since.pop(port, None)
                continue
            t0 = self.bad_since.setdefault(port, now)
            inc = self.incident_of(port)
            if inc:
                inc["ports"][str(port)]["state"] = st
                continue
            if st == "lost" and self.args.heal:
                h = self.heal.get(port)
                if not h or not h.get("err"):
                    continue  # healing first
            if now - t0 < self.args.incident_grace:
                continue
            self.open_incident(r)

    def open_incident(self, r):
        port, host, st = r["port"], r["host"], r["state"]
        with self.inc_lock:
            inc = next((i for i in self.incidents.values() if i["host"] == host), None)
            new = inc is None
            if new:
                self.inc_seq += 1
                inc = {"id": self.inc_seq, "host": host, "ports": {}, "opened": utc_now(), "state": "diagnosing", "diag": {},
                       "steps": [], "human": [], "hands": [], "auto_done": [], "checks": 0, "reminded": 0,
                       "next_check": 0, "queued": False, "probing": False}
                self.incidents[inc["id"]] = inc
            was = self.good_state.get(port) or ("kept open" if r.get("want") else "?")
            inc["ports"][str(port)] = {"state": st, "was": was, "since": utc_now(), "remote": f"{r['remote_ip']}:{r['remote_port']}",
                                       "service": r.get("service") or r.get("notes") or ""}
            self.save_incidents()
        msg = (f"🚨 incident #{inc['id']}: {port} {was} → {st} · {host} {r['remote_ip']}:{r['remote_port']} {r.get('notes') or ''}".rstrip()
               + (" — diagnosing…" if new else f" (joins #{inc['id']})"))
        chat(msg, "ErrorMsg")
        emit({"t": "alert", "text": clip(msg, 200)})
        self.mem.event("incident", host=host, port=port, detail=f"#{inc['id']} opened: {was} → {st}")
        self.enqueue_incident(inc)

    def enqueue_incident(self, inc):
        if inc.get("queued") or inc["id"] not in self.incidents:
            return
        inc["queued"] = True
        self.tasks.put(("\x00incident", inc["id"], None))

    @staticmethod
    def row_state(rows, port) -> str:
        r = next((x for x in rows if x["port"] == int(port)), None)
        return r["state"] if r else "removed"

    def check_resolved(self, inc, rows) -> bool:
        if inc["id"] not in self.incidents:
            return True
        states = {p: self.row_state(rows, p) for p in inc["ports"]}
        good = all(st in ("up", "loading", "local") for st in states.values())
        gone = all(st in ("up", "loading", "local", "removed", "off", "disabled") for st in states.values())
        if good or gone:
            self.resolve_incident(inc, states, dismissed=not good)
            return True
        for p, st in states.items():
            inc["ports"][p]["state"] = st
        return False

    def incident_tick(self, rows):
        now = time.time()
        for inc in list(self.incidents.values()):
            if inc.get("busy"):
                continue  # its handler is at work (and resolves it itself): the fix is credited to what it did
            if self.check_resolved(inc, rows) or inc.get("queued") or inc.get("probing"):
                continue
            if inc["state"] == "hands" and inc.get("hands_at") and inc.get("reminded", 0) < 3 \
                    and now - core.epoch(inc["hands_at"]) > self.args.hands_remind * 60 * (inc.get("reminded", 0) + 1):
                inc["reminded"] = inc.get("reminded", 0) + 1
                txt = f"⏰ incident #{inc['id']} still needs you: {inc['host']} — {clip((inc.get('diag') or {}).get('cause'), 140)} (===i: the checklist)"
                chat(txt, "WarningMsg")
                emit({"t": "alert", "text": txt})
            if now < inc.get("next_check", 0):
                continue
            if inc["state"] == "hands" and (inc.get("diag") or {}).get("layer") in HOST_LAYERS:
                inc["probing"] = True
                threading.Thread(target=self._probe_back, args=(inc,), daemon=True).start()
            else:
                self.enqueue_incident(inc)

    def _probe_back(self, inc):
        """While waiting for the human: a cheap look (resolve + TCP to ssh) every 20 s; when the host answers again,
        healing restarts and the incident is handled again."""
        try:
            host = inc["host"]
            cfg = pinv.ssh_config(host, self.args.ssh)
            ips, _ = pinv.resolve(cfg.get("hostname") or host, 3)
            ips = ips or pinv.all_ips(self.mem.host(host).get("inventory") or {})[:1]
            sport = int(cfg.get("port") or 22)
            if any(pinv.tcp(ip, sport, 2.5) == "open" for ip in ips[:2]):
                chat(f"   👀 #{inc['id']}: {host} answers ssh again — checking", "DiagnosticOk")
                for p in inc["ports"]:
                    self.heal.pop(int(p), None)
                inc["next_check"] = 0
                self.enqueue_incident(inc)
            else:
                inc["next_check"] = time.time() + 20
        except Exception as e:
            logw(f"probe #{inc.get('id')}: {type(e).__name__}: {e}", "ErrorMsg")
            inc["next_check"] = time.time() + 60
        finally:
            inc["probing"] = False

    def run_incident(self, iid):
        inc = self.incidents.get(iid)
        if not inc:
            return
        inc["queued"] = False
        self.current = (f"incident #{iid}", None)
        self.cancel.clear()
        self.set_phase(f"incident #{iid}")
        inc["busy"] = True
        try:
            self.handle_incident(inc)
        except Cancelled:
            chat(f"  ✖ incident #{iid}: cancelled (still watched)", "WarningMsg")
            inc["next_check"] = time.time() + 120
        except Exception as e:
            chat(f"  ✖ incident #{iid}: {type(e).__name__}: {e}", "ErrorMsg")
            inc["next_check"] = time.time() + 120
        finally:
            inc["busy"] = False
            self.current = None
            self.save_incidents()
            self.set_phase("idle")
            self.emit_dash()

    def handle_incident(self, inc):
        """Diagnose, fix what ptrm can (tunnel; the service from its recipe, approved), else ask for remote hands."""
        host = inc["host"]
        rows = self.fresh_rows(0)
        if self.check_resolved(inc, rows):
            return
        bad = [int(p) for p in inc["ports"] if self.row_state(rows, p) in INC_BAD]
        e = self.table.get(bad[0] if bad else int(next(iter(inc["ports"]))))
        if not e:
            return
        inc["checks"] = inc.get("checks", 0) + 1
        self.set_phase(f"incident #{inc['id']}: diagnosing")
        dg = pinv.diagnose(host, self.mem.host(host), self.args.ssh, e.rport, e.kind, self.tun.log_tail(e.lport, 8))
        prev = (inc.get("diag") or {}).get("layer")
        inc["diag"] = dg
        if dg["facts"].get("ssh") == "ok":
            self.mem.touch_host(host, True, via="diagnose")
        elif dg["layer"] in HOST_LAYERS:
            self.mem.touch_host(host, False, err=dg["cause"], via="diagnose")
        if dg["layer"] != prev:
            chat(f"🔎 #{inc['id']} {dg['layer']}: {dg['cause']}", "WarningMsg")
            for x in dg["evidence"][:6]:
                chat(f"   · {x}", "Comment")
        layer = dg["layer"]
        if layer in ("tunnel", "ok"):
            for p in bad:
                ee = self.table.get(p)
                self.heal.pop(p, None)
                if self.tun.scan().get(p):
                    self.close_entry(p, asked=True)
                    time.sleep(0.3)
                out = self.open_entry(ee, how="heal")
                ok = not out.startswith("Error")
                inc["steps"].append(("" if ok else "✖ ") + f"reopened the tunnel {p}: {clip(out.splitlines()[0], 120)}")
                chat(f"   🩹 {inc['steps'][-1]}", "DiagnosticOk" if ok else "ErrorMsg")
        elif layer == "service" and f"launch {e.rport}" not in inc["auto_done"]:
            inc["auto_done"].append(f"launch {e.rport}")
            chat(f"   ⚙ relaunching {e.service or 'the service'} on {host} (port {e.rport}) from its recipe", "Function")
            self.approve_timeout = self.args.incident_approve_timeout
            try:
                out = self.launch({"host": host, "port": e.lport}, None)
            finally:
                self.approve_timeout = None
            ok = out.startswith(("launched", "already running"))
            inc["steps"].append(("" if ok else "✖ ") + f"relaunch {e.service or e.rport} from its recipe: {clip(out.splitlines()[0], 150)}")
            chat(f"   {'✔' if ok else '✖'} {clip(out, 500)}", "DiagnosticOk" if ok else "ErrorMsg")
            if not ok and "no recipe" in out:
                self.start_teach(e, f"incident #{inc['id']}: the service is down and I do not know how to start it")
                inc["steps"].append(f"opened a teach terminal on {host} for {e.lport}")
            if ok:
                for p in bad:
                    if not self.tun.scan().get(p):
                        self.heal.pop(p, None)
                        self.open_entry(self.table.get(p), how="heal")
        if self.check_resolved(inc, self.fresh_rows(0)):
            return
        if layer == "loading":
            inc["state"], inc["next_check"] = "watching", time.time() + 20
            chat(f"   ⏳ #{inc['id']}: the service is loading; I look again in 20 s", "Comment")
            return
        if any(int(p) in self.teaching for p in inc["ports"]):
            inc["state"], inc["next_check"] = "teaching", time.time() + 60
            return
        if dg["hands"] or layer not in ("tunnel", "ok"):
            if time.time() - inc.get("asked_at", 0) < 300:  # the LLM just asked: merge, do not post a second list
                keep = [x for x in inc.get("hands") or [] if not x.startswith("(learned)")]
                self.request_hands(inc, dg, changed=False, extra=keep)
            else:
                self.request_hands(inc, dg, changed=layer != prev)
        else:
            inc["state"] = "watching"
        inc["next_check"] = time.time() + min(300, 20 * 2 ** min(inc["checks"] - 1, 4))

    def request_hands(self, inc, dg, changed=True, extra=None):
        """Ask the user (physical access) for help: the checklist from the diagnosis, the host's runbook, past cases."""
        host = inc["host"]
        h = self.mem.host(host)
        steps = list(extra or []) + [x for x in dg.get("hands") or [] if x not in (extra or [])]
        learned = [r.get("step") for r in h.get("runbook") or [] if r.get("step")][-4:]
        inc["hands"] = steps + [f"(learned) {x}" for x in learned]
        first = inc["state"] != "hands"
        inc["state"] = "hands"
        if not (first or changed):
            return
        inc["hands_at"] = utc_now()
        past = [c for c in self.mem.cases(host=host) if c.get("kind") == "incident" and c.get("outcome") == "resolved" and c.get("fix")]
        same = [c for c in past if c.get("layer") == dg.get("layer")][-2:] or past[-1:]
        ports = ", ".join(f"{p} {v['state']}" for p, v in inc["ports"].items())
        lines = [f"🖐 remote hands needed — {host} · incident #{inc['id']}: {ports} since {when(inc['opened'])}",
                 f"   {dg.get('layer')}: {dg.get('cause')}"]
        if inc["steps"]:
            lines.append("   I tried: " + " · ".join(clip(x, 110) for x in inc["steps"][-3:]))
        lines.append("   Please check, then tell me what you did (just type it; ===d <text> works without the LLM):")
        lines += [f"    {i}. {x}" for i, x in enumerate(inc["hands"], 1)]
        lines += [f"   last time ({when(c['ts'])}, {c.get('layer')}): {clip(c['fix'], 160)}" for c in same]
        chat("\n".join(lines), "WarningMsg")
        emit({"t": "alert", "text": f"remote hands needed: {host} — {clip(dg.get('cause'), 140)} (incident #{inc['id']})"})
        emit({"t": "show"})
        self.mem.event("hands", host=host, port=int(next(iter(inc["ports"]))) if inc["ports"] else None,
                       detail=f"#{inc['id']} {dg.get('layer')}: {clip(dg.get('cause'), 200)}")
        self.save_incidents()

    def hands_reply(self, text: str, inc=None) -> Optional[dict]:
        """The user says what they did: attach it to the incident waiting for hands, recheck now."""
        inc = inc or next(iter(sorted((i for i in self.incidents.values() if i["state"] == "hands"),
                                       key=lambda i: i.get("hands_at") or "", reverse=True)), None) \
            or next(iter(self.incidents.values()), None)
        if not inc:
            chat("  no open incident", "Comment")
            return None
        if text:
            inc["human"].append({"at": utc_now(), "text": clip(text, 300)})
        inc["next_check"] = 0
        for p in inc["ports"]:
            self.heal.pop(int(p), None)
        self.save_incidents()
        chat(f"  🖐 noted for incident #{inc['id']} ({inc['host']}); checking again…", "Comment")
        self.enqueue_incident(inc)
        return inc

    def resolve_incident(self, inc, states, dismissed=False):
        with self.inc_lock:
            if inc["id"] not in self.incidents:
                return
            self.incidents.pop(inc["id"])
        self.save_incidents()
        secs = time.time() - core.epoch(inc["opened"])
        dg = inc.get("diag") or {}
        human = [x["text"] for x in inc.get("human") or []]
        did = [x for x in inc.get("steps") or [] if not x.startswith("✖")]
        lost = any(v.get("state") == "lost" or v.get("was") == "kept open" for v in inc["ports"].values())
        if dismissed:
            fix, outcome = "closed or removed by the user", "dismissed"
        elif human:
            fix, outcome = "human: " + " / ".join(clip(x, 120) for x in human[-3:]), "resolved"
        elif did:
            fix, outcome = "; ".join(did[-2:]), "resolved"
        elif lost and dg.get("layer") in (None, "tunnel", "ok"):
            fix, outcome = "healed: the tunnel was reopened", "resolved"
        else:
            fix, outcome = (f"came back by itself after {core.fmt_secs(secs)}"
                            + (" (the host woke up / came back on the network)" if dg.get("layer") in HOST_LAYERS else "")), "resolved"
        ports = [int(p) for p in inc["ports"]]
        svc = ", ".join(sorted({v.get("service") for v in inc["ports"].values() if v.get("service")}))
        symptom = "; ".join(f"{p} {v.get('was')} → {v.get('state')}" for p, v in inc["ports"].items())
        self.mem.case("incident", id=inc["id"], host=inc["host"], ports=ports, service=svc, symptom=symptom,
                      layer=dg.get("layer"), cause=dg.get("cause"), evidence=(dg.get("evidence") or [])[:6],
                      steps=inc.get("steps"), human=human, hands=inc.get("hands"), fix=fix, outcome=outcome, secs=round(secs))
        learned = []
        if not dismissed:
            for p, v in inc["ports"].items():
                self.mem.port_failed(int(p), inc["host"], v.get("remote", ""), v.get("service", ""), dg.get("layer") or "unknown",
                                     dg.get("cause") or v.get("state", ""))
                self.mem.port_fixed(int(p), inc["host"], v.get("remote", ""), v.get("service", ""), fix, secs)
            self.mem.host_incident(inc["host"], dg.get("layer") or "unknown", dg.get("cause") or "")
            for t in human:
                if len(t) >= 10 and not ACK.fullmatch(t) and PHYSICAL.search(t):
                    self.mem.runbook_add(inc["host"], t, f"incident #{inc['id']}")
                    learned.append(t)
        self.mem.event("incident", host=inc["host"], port=ports[0] if ports else None, ok=not dismissed,
                       detail=f"#{inc['id']} {outcome}: {dg.get('layer')}: {clip(dg.get('cause'), 160)} → {fix} ({core.fmt_secs(secs)})")
        if dismissed:
            chat(f"incident #{inc['id']} closed ({fix}); kept as a case.", "Comment")
        else:
            chat(f"✅ incident #{inc['id']} resolved after {core.fmt_secs(secs)}: {fix}. Kept in memory: the case, the port's profile"
                 + (", the host's runbook" if learned else "") + ".", "DiagnosticOk")
            emit({"t": "alert", "text": f"incident #{inc['id']} resolved: {inc['host']} ({clip(fix, 100)})"})
            if dg.get("layer") in HOST_LAYERS or (dg.get("facts") or {}).get("rebooted"):
                threading.Thread(target=self._learn_after, args=(inc,), daemon=True).start()

    def dismiss_incident(self, inc, why: str = ""):
        """The user closes an incident: ptrm stops keeping its ports open (they go off) and stops watching them."""
        for p in inc["ports"]:
            self.tun.set_want(int(p), False)
            self.heal.pop(int(p), None)
            self.bad_since.pop(int(p), None)
            self.ever_up.discard(int(p))
        self.resolve_incident(inc, {}, dismissed=True)
        self.wake.set()

    def _learn_after(self, inc):
        """After a host-level incident: look at the host again and say what changed (rebooted, other network, GPU …)."""
        host = inc["host"]
        try:
            h = self.mem.discovered(host, core.discover(host, self.args.ssh))
            ch = h.get("_new_changes") or []
            if ch:
                txt = "; ".join(("⚠ " if c["sev"] else "") + c["what"] for c in ch[:6])
                chat(f"   🔁 {host} since the last look: {txt}", "WarningMsg")
                self.mem.event("note", host=host, detail=f"after incident #{inc['id']}: {txt}")
            self.emit_dash()
        except Exception as e:
            logw(f"inventory after #{inc['id']}: {type(e).__name__}: {e}", "ErrorMsg")

    def incident_brief(self, inc) -> str:
        dg = inc.get("diag") or {}
        ports = ", ".join(f"{p} {v['state']} (was {v.get('was')}, {v.get('service') or v.get('remote')})" for p, v in inc["ports"].items())
        out = [f"#{inc['id']} {inc['host']}: {ports} since {when(inc['opened'])} · {inc['state']}"]
        if dg:
            out.append(f"  {dg.get('layer')}: {dg.get('cause')}")
            out += [f"  · {x}" for x in (dg.get("evidence") or [])[:4]]
        if inc.get("steps"):
            out.append("  ptrm did: " + " · ".join(clip(x, 100) for x in inc["steps"][-3:]))
        if inc.get("hands"):
            out.append("  asked the user: " + " | ".join(f"{i}) {clip(x, 90)}" for i, x in enumerate(inc["hands"], 1)))
        if inc.get("human"):
            out.append("  the user said: " + " | ".join(clip(x["text"], 120) for x in inc["human"][-3:]))
        return "\n".join(out)

    def incident_command(self, arg: str):
        arg = arg.strip()
        if arg.startswith("x"):
            iid = int(re.sub(r"\D", "", arg[1:]) or 0)
            inc = self.incidents.get(iid) if iid else None
            if not inc:
                return chat(f"  no open incident {arg[1:].strip()}", "Comment")
            return self.dismiss_incident(inc)
        if not self.incidents:
            return chat("  no open incident", "Comment")
        for inc in self.incidents.values():
            if arg and str(inc["id"]) != arg:
                continue
            chat(self.incident_brief(inc), "WarningMsg")
        chat("  ===d <what you did> (recheck now) · ===i x <id> close one", "Comment")

    def inventory_tick(self, rows):
        """Every 10 min: look again (discovery + inventory) at a host whose tunnel works and whose inventory is older
        than --inventory-age hours, so a change (moved, rebooted, GPU gone, disk filling) is spotted before it bites."""
        if not self.args.inventory_age or self.inv_running or time.time() < self.next_inv:
            return
        self.next_inv = time.time() + 600
        hosts = []
        for r in rows:
            if r["state"] in ("up", "loading", "down", "stalled") and r.get("tunnel") and r["host"] not in hosts \
                    and not r.get("disabled") and not r.get("host_off"):
                hosts.append(r["host"])
        for h in hosts:
            d = self.mem.host(h)
            if not d.get("inventory") or time.time() - core.epoch(d.get("discovered")) > self.args.inventory_age * 3600:
                self.inv_running = True
                threading.Thread(target=self._inventory_one, args=(h,), daemon=True).start()
                return

    def _inventory_one(self, host):
        try:
            d = core.discover(host, self.args.ssh)
            h = self.mem.discovered(host, d)
            ch = h.get("_new_changes") or []
            if ch:
                chat(f"🔁 {host} changed since the last look: " + "; ".join(("⚠ " if c["sev"] else "") + c["what"] for c in ch[:6]),
                     "WarningMsg" if any(c["sev"] for c in ch) else "Comment")
            elif d.get("ok"):
                logw(f"inventory {host}: profile {h.get('profile')}, no change", "Comment")
            self.emit_dash()
        except Exception as e:
            logw(f"inventory {host}: {type(e).__name__}: {e}", "ErrorMsg")
        finally:
            self.inv_running = False

    @staticmethod
    def format_diag(dg: dict) -> str:
        out = [f"diagnosis — {dg['layer']}: {dg['cause']}"] + [f"  · {x}" for x in dg.get("evidence") or []]
        if dg.get("auto"):
            out.append("  ptrm can: " + ", ".join(dg["auto"]))
        if dg.get("hands"):
            out.append("  for a human at the host:")
            out += [f"    {i}. {x}" for i, x in enumerate(dg["hands"], 1)]
        return "\n".join(out)

    def diagnose_text(self, host: str, rport=None, kind: str = "", lport=None) -> str:
        dg = pinv.diagnose(host, self.mem.host(host), self.args.ssh, rport, kind, self.tun.log_tail(lport, 8) if lport else "")
        if dg["facts"].get("ssh") == "ok":
            self.mem.touch_host(host, True, via="diagnose")
        elif dg["layer"] in HOST_LAYERS:
            self.mem.touch_host(host, False, err=dg["cause"], via="diagnose")
        rb = [r.get("step") for r in self.mem.host(host).get("runbook") or []][-4:]
        past = [c for c in self.mem.cases(host=host) if c.get("kind") == "incident" and c.get("layer") == dg["layer"]][-2:]
        return (self.format_diag(dg) + ("\n  runbook of the host: " + " | ".join(rb) if rb else "")
                + "".join(f"\n  past case: {self.mem.case_line(c)}" for c in past))

    # ----- bringing the table up: the rows of ptrm.md are what the user wants at those ports
    def model_mismatch(self, e: Entry, model: str) -> str:
        """For an llm/ row: the model that answers does not match NOTES (e.g. a 12b where NOTES says e2b)."""
        if not model or e.kind != "llm" or not core.notes_clear(e.notes):
            return ""
        sc, _ = core.note_match(e.notes, model)
        if sc >= 0.5:
            return ""
        if e.host and any(int(f["recipe"].get("port") or 0) == e.rport and f["score"] >= 0.5
                          for f in self.mem.find_recipes(e.notes, e.rport, e.host)):
            return ""  # NOTES names what was taught/recalled for this port (a launcher, an alias), not the model
        return f"{e.lport} answers with {model}, NOTES asks for {e.notes}"

    def choose_host(self, e: Entry, exclude=()) -> Optional[tuple]:
        """A host for a row whose SSH_HOST is empty: one that serves or knows how to start what NOTES says (recipes,
        procedures), served this port before, has the capacity (VRAM/RAM for llm/), answered last time, has no
        incident — and answers on ssh now. (host, remote port or None, why)."""
        cands = []
        port_hist = {m.get("host"): m for m in self.mem.port(e.lport).get("profiles") or []}
        for h in self.mem.known_hosts():
            if h in exclude or h in self.off_hosts:
                continue
            d = self.mem.host(h)
            inv = d.get("inventory") or {}
            if not inv and not (d.get("os") or {}).get("system"):
                continue
            score, why, rport = 0.0, [], None
            found = self.mem.find_recipes(e.notes, e.rport, h)
            serving = {int(x.get("port") or 0): x for x in d.get("services") or []}
            if found:
                f = found[0]
                score += 3 * f["score"]
                why.append(f"{f['recipe'].get('service')} ({f['why']})")
                rp = int(f["recipe"].get("port") or 0)
                if e.auto_rport and rp in serving:
                    rport, score = rp, score + 2
                    why.append(f"serving it now on {rp}")
            elif e.kind and (serving.get(e.rport) or {}).get("service", "").startswith(e.kind + "/"):
                score += 1
                why.append(f"serves a {e.kind}/ on {e.rport}")
            if (port_hist.get(h) or {}).get("opens") or (port_hist.get(h) or {}).get("ups"):
                score += 1
                why.append(f"served {e.lport} before")
            gb = [int(g.get("vram_gb") or 0) for g in inv.get("gpus") or []]
            mem_gb = (inv.get("mem") or {}).get("total_gb") or 0
            score += (min(max(gb or [0]), 48) / 16 + mem_gb / 128) if e.kind == "llm" else mem_gb / 256
            err = d.get("last_error") or {}
            score += 0.5 if d.get("last_ok") and (not err or err.get("at", "") <= d["last_ok"]) else -1
            if self.incident_of(host=h):
                score -= 3
            if not why:
                why.append("most capacity: " + " ".join(f"{k} {v}" for k, v in pinv.chips(inv, d)))
            cands.append((score, h, rport, "; ".join(why)))
        cands.sort(key=lambda c: -c[0])
        for score, h, rport, why in cands[:4]:
            cfg = pinv.ssh_config(h, self.args.ssh)
            ips, _ = pinv.resolve(cfg.get("hostname") or h, 3)
            ips = ips or pinv.all_ips(self.mem.host(h).get("inventory") or {})[:1]
            if any(pinv.tcp(ip, int(cfg.get("port") or 22), 2.5) == "open" for ip in ips[:2]):
                return h, rport, why
        return None

    def enqueue_bringup(self, port: int, auto: bool = False):
        """auto: ptrm's own initiative (start, a row or host enabled again): a row on a disabled host is skipped."""
        self.bringing.add(int(port))
        self.tasks.put(("\x00bringup", int(port), {"auto": auto}))

    def run_bringup(self, port, auto: bool = False):
        self.current = (f"bring up {port}", None)
        self.cancel.clear()
        self.set_phase(f"bring up {port}")
        try:
            res = self.bring_up(port, auto=auto)
            e = self.table.get(port)
            if e and e.host and res not in ("host down", "no host", "not in the table", "busy", "other", "host disabled"):
                self.host_monitor(e.host)
        except Cancelled:
            chat(f"  ✖ bring-up of {port} cancelled", "WarningMsg")
        except Exception as ex:
            chat(f"  ✖ bring-up of {port}: {type(ex).__name__}: {ex}", "ErrorMsg")
        finally:
            self.bringing.discard(port)
            self.current = None
            self.set_phase("idle")
            self.emit_dash()

    def bring_up(self, port: int, exclude=(), auto: bool = False) -> str:
        """The row's service at localhost:<port>: choose the host (SSH_HOST empty), know the host (inventory), open the
        tunnel, start the service from what NOTES recalls (a recipe, a taught procedure), else ask the user in a
        teach terminal. Every step is kept (port profile, case)."""
        e = self.table.get(port)
        if not e:
            return "not in the table"
        rows = self.fresh_rows(2)
        r = next((x for x in rows if x["port"] == port), {})
        st, pr = r.get("state"), r.get("probe") or {}
        what = f"{port} {e.notes or '(no NOTES)'}"
        if e.disabled:
            chat(f"🚫 {port} is disabled: bringing it up because you asked; I do not keep it (===on {port} enables it)", "Comment")
        if e.host and e.host in self.off_hosts and not e.auto_host:
            if auto:
                chat(f"🚫 {port}: its host {e.host} is disabled: I leave it alone (Xh on the host enables it)", "Comment")
                return "host disabled"
            chat(f"🚫 {e.host} is disabled: bringing {port} up there because you asked", "Comment")
        if st in ("up", "loading", "local"):
            if (r.get("tunnel") or {}).get("managed"):
                self.tun.set_want(port, True)
            mm = self.model_mismatch(e, pr.get("model", ""))
            chat(f"✔ {what}: up" + (f" · {pr['model']}" if pr.get("model") else "") + (f"\n   ⚠ {mm}" if mm else ""),
                 "WarningMsg" if mm else "DiagnosticOk")
            return "up"
        if st == "busy":
            holder = ", ".join(f"{h['name']} pid {h['pid']}" for h in r.get("holders") or [])
            chat(f"⛔ {what}: the port is held by {holder}; I leave it alone", "WarningMsg")
            return "busy"
        if st == "other":
            t = r.get("tunnel") or {}
            chat(f"🔀 {what}: a tunnel to {t.get('host')} {t.get('rip')}:{t.get('rport')} holds it; I leave it alone (R reopens it to the row)", "WarningMsg")
            return "other"
        chat(f"⏫ bringing up {what}", "Title")
        steps = []
        if e.auto_host and (not e.host or e.host in exclude or e.host in self.off_hosts):
            pick = self.choose_host(e, exclude)
            if not pick:
                chat(f"   ✖ SSH_HOST is empty and no known host answers that can take it — name a host in the table, "
                     "or discover one (d on a HOSTS line)", "ErrorMsg")
                return "no host"
            self.mem.assign(port, pick[0], pick[1], pick[2])
            e = self.table.get(port)
            steps.append(f"chose {e.host} ({pick[2]})")
            chat(f"   🧭 SSH_HOST is empty: I chose {e.host} — {pick[2]}", "Comment")
        h = self.mem.host(e.host)
        fresh = h.get("inventory") and time.time() - core.epoch(h.get("discovered")) < max(1.0, self.args.inventory_age or 24) * 3600
        if not fresh:
            chat(f"   🔍 looking at {e.host} (inventory, services, recipes)…", "Comment")
            d = core.discover(e.host, self.args.ssh)
            h = self.mem.discovered(e.host, d)
            if not d.get("ok"):
                if e.auto_host:
                    alt = self.choose_host(e, set(exclude) | {e.host})
                    if alt:
                        chat(f"   ✖ {e.host} does not answer ({clip(d.get('err'), 100)}); trying {alt[0]}", "WarningMsg")
                        self.mem.assign(port, None)
                        return self.bring_up(port, set(exclude) | {e.host}, auto)
                self.tun.set_want(port, True, e)  # kept open: healing tries, the incident diagnoses and asks for hands
                chat(f"   ✖ {e.host} does not answer: {clip(d.get('err'), 160)} — I keep {port} wanted; the incident "
                     "handling diagnoses it", "ErrorMsg")
                return "host down"
            steps.append(f"inventory of {e.host}")
        out = self.open_entry(e, how="bringup")
        if out.startswith("Error"):
            chat(f"   ✖ {clip(out, 300)}", "ErrorMsg")
            return "open failed"
        steps.append("open the tunnel")
        pr = core.probe(port, e.kind, self.args.probe_timeout)
        if pr["state"] in ("ok", "loading", "open"):
            mm = self.model_mismatch(e, pr.get("model", ""))
            chat(f"   ✔ {port} up through {e.host} {e.remote}" + (f" · {pr['model']}" if pr.get("model") else "")
                 + (f"\n   ⚠ {mm}" if mm else ""), "WarningMsg" if mm else "DiagnosticOk")
            self.mem.port_bringup(port, e.host, e.remote, e.service, steps, pr.get("model", ""))
            return "up"
        # the tunnel works, nothing answers behind it: start the service
        found = self.mem.find_recipes(e.notes, e.rport, e.host)
        if found and found[0]["recipe"].get("launch"):
            f = found[0]
            chat(f"   ⚙ NOTES recalls {f['recipe'].get('service')} on {e.host} ({f['why']}): launching it", "Function")
            self.approve_timeout = self.args.incident_approve_timeout
            try:
                lo = self.launch({"host": e.host, "port": port, "recipe": f["recipe"], "service": e.service or f["recipe"].get("service")}, None)
            finally:
                self.approve_timeout = None
            ok = lo.startswith(("launched", "already running"))
            chat(f"   {'✔' if ok else '✖'} {clip(lo, 500)}", "DiagnosticOk" if ok else "ErrorMsg")
            if ok:
                steps.append(f"launch {f['recipe'].get('service')} from {f['recipe'].get('source', 'recipe')} ({f['why']})")
                pr = core.probe(port, e.kind, self.args.probe_timeout)
                self.mem.port_bringup(port, e.host, e.remote, e.service, steps, pr.get("model", ""))
                self.mem.case("bringup", host=e.host, ports=[port], service=e.service, request=f"bring up {e.notes}",
                              steps=steps, fix=f"recipe {clip(f['recipe'].get('launch'), 160)}", outcome="ok")
                return "up"
            if lo.startswith("Error"):
                self.mem.recipe_failed(e.host, f["recipe"], lo.splitlines()[0])
            return self.start_teach(e, f"the recalled recipe failed: {clip(lo.splitlines()[0], 140)}", self.teach_hints(e))
        if not core.notes_clear(e.notes):
            why = f"NOTES ({e.notes or 'empty'}) does not say which service is expected"
        elif self.mem.find_recipes(e.notes, e.rport):
            why = f"NOTES recalls a recipe on another host only; I never started it on {e.host}"
        else:
            why = f"NOTES ({e.notes}) recalls no recipe or procedure on any host"
        return self.start_teach(e, why, self.teach_hints(e))

    # ----- teach: the user brings the service up in a terminal on the host; ptrm records it and learns
    def _teach_path(self) -> str:
        return os.path.join(self.mem.run_dir, "teach.json")

    def load_teaching(self):
        try:
            with open(self._teach_path(), encoding="utf-8") as f:
                self.teaching = {int(k): dict(v, busy=False, next=0) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.teaching = {}

    def save_teaching(self):
        try:
            with open(self._teach_path(), "w", encoding="utf-8") as f:
                json.dump({str(k): dict(v, busy=False) for k, v in self.teaching.items()}, f, indent=1)
        except OSError:
            pass

    def teach_hints(self, e: Entry) -> list:
        out = []
        for f in self.mem.find_recipes(e.notes, e.rport)[:2]:
            out.append(f"{f['host']} knows {f['recipe'].get('service')}: {clip(f['recipe'].get('launch'), 150)}")
        for m in (self.mem.port(e.lport).get("profiles") or []):
            if m.get("bringup") and m.get("host") == e.host:
                out.append("last time: " + " → ".join(clip(x, 60) for x in m["bringup"][-4:]))
        return out

    def teach_cmd(self, port: int, host: str) -> list:
        """A plain login shell on the host; the setup is typed into it (teach_input): with a terminal, some sshd
        (Win32-OpenSSH with Git Bash) drop the command given to ssh -t."""
        return [self.args.ssh, "-t", host]

    @staticmethod
    def teach_input(port: int) -> str:
        return f"exec bash --rcfile ~/.ptrm/teach-{int(port)}.rc -i\n"

    def start_teach(self, e: Entry, why: str, hints=()) -> str:
        """Open a terminal on the row's host in STATS and ask the user to bring the service up there. The commands
        they type are recorded (bash history of that shell); when the service answers, ptrm learns the recipe (the
        command line of the process on the port) and the procedure (the commands)."""
        port, host = e.lport, e.host
        if not host:
            chat(f"   ✖ {port}: no host to open a terminal on (SSH_HOST empty, none chosen)", "ErrorMsg")
            return "no host"
        if port in self.teaching:
            emit({"t": "stats", "key": f"teach:{port}", "host": host, "tool": "teach", "cmd": self.teach_cmd(port, host),
                  "input": self.teach_input(port), "title": f"teach {port} · {host.split('@')[-1]}", "teach": True, "focus": True,
                  "name": self.term_name(host)})
            return "teaching"
        windows = bool(self.mem.host(host).get("windows"))
        banner = [f"ptrm · teach {port}: please bring up '{e.notes or 'the service'}' on this host so that it answers on 127.0.0.1:{e.rport}",
                  f"  (localhost:{port} on {socket.gethostname()} reaches it through the tunnel). Why I ask: {why}.",
                  "  Every command you type here is recorded; when the service answers, I keep it as the recipe and the",
                  "  procedure, and launch it myself next time. Leave it running here, or type exit once it answers",
                  "  (it stops with this shell; I relaunch it detached from what I learnt)."] + [f"  hint: {x}" for x in hints][:7]
        rc = "\n".join([
            "[ -f ~/.bashrc ] && . ~/.bashrc",
            f"HISTFILE=~/.ptrm/teach-{port}.hist; HISTSIZE=5000; HISTFILESIZE=5000; HISTTIMEFORMAT='%s '",
            "shopt -s histappend 2>/dev/null",
            "PROMPT_COMMAND='history -a'",
            f"PS1='\\[\\e[1;33m\\][ptrm teach {port}]\\[\\e[0m\\] \\w \\$ '",
            "printf '\\033[1;33m%s\\033[0m\\n' " + " ".join(shlex.quote(x) for x in banner)])
        script = ((core.WINPREP if windows else "") + "mkdir -p ~/.ptrm\n"
                  + f"cat > ~/.ptrm/teach-{port}.rc <<'PTRM_EOF'\n{rc}\nPTRM_EOF\n: > ~/.ptrm/teach-{port}.hist\necho PTRM-OK\n")
        r = core.ssh_run(host, script, 30, self.args.ssh)
        if "PTRM-OK" not in r["out"]:
            err = core.ssh_error(r["err"], r["rc"])
            chat(f"   ✖ cannot open a teach terminal on {host}: {err}", "ErrorMsg")
            self.tun.set_want(port, True, e)
            return "host down"
        self.teaching[port] = {"host": host, "rport": e.rport, "service": e.service, "notes": e.notes, "kind": e.kind,
                               "why": why, "started": utc_now(), "next": 0, "seen": 0, "cmds": [], "busy": False,
                               "windows": windows}
        self.save_teaching()
        emit({"t": "stats", "key": f"teach:{port}", "host": host, "tool": "teach", "cmd": self.teach_cmd(port, host),
              "input": self.teach_input(port), "title": f"teach {port} · {host.split('@')[-1]}", "teach": True, "focus": True,
              "name": self.term_name(host)})
        self.stats_seen.add(host)  # the teach terminal is this host's pane
        chat(f"🎓 {port} {e.notes or ''}: I don't know how to start it on {host} — {why}.\n"
             f"   I opened a terminal there (STATS, 'teach {port}'): bring it up so it answers on 127.0.0.1:{e.rport}; "
             "I record the commands and learn the recipe." + "".join(f"\n   hint: {x}" for x in hints), "WarningMsg")
        emit({"t": "alert", "text": f"ptrm: please show me how to start {e.notes or port} on {host} (teach terminal in STATS)"})
        self.mem.event("teach", port=port, host=host, service=e.service, detail=f"asked the user: {why}")
        return "teaching"

    TRIVIAL = re.compile(r"^\s*(ls|ll|la|dir|pwd|clear|cls|history|exit|logout|cat|less|more|head|tail|man|which|type|"
                         r"whoami|top|htop|btop|nvtop|nvidia-smi|ps|df|du|free|curl|wget\s+-q\s+-O-|echo|true|:)\b")

    def teach_tick(self):
        now = time.time()
        for port, t in list(self.teaching.items()):
            if t.get("busy") or now < t.get("next", 0):
                continue
            if now - core.epoch(t["started"]) > self.args.teach_hours * 3600:
                self.stop_teach(port, "no service after %g h" % self.args.teach_hours)
                continue
            t["busy"] = True
            threading.Thread(target=self._teach_poll, args=(port,), daemon=True).start()

    def _teach_poll(self, port: int, final: bool = False):
        t = self.teaching.get(port)
        if not t:
            return
        try:
            path = "/health" if t.get("kind") == "llm" else "/"
            r = core.ssh_run(t["host"], f"cat ~/.ptrm/teach-{port}.hist 2>/dev/null; echo @@CODE; curl -s -m 4 -o /dev/null "
                                        f"-w '%{{http_code}}' http://127.0.0.1:{int(t['rport'])}{path}; echo\n", 25, self.args.ssh)
            if r["rc"] == 255:
                t["next"] = time.time() + 30
                return
            hist, _, code = r["out"].partition("@@CODE")
            cmds = [l for l in hist.splitlines() if l.strip() and not re.match(r"^#\d+$", l.strip())]
            for c in cmds[t.get("seen", 0):]:
                chat(f"   🎓 {port} $ {clip(c, 200)}", "Comment")
            t["seen"], t["cmds"] = len(cmds), cmds
            code = code.strip()
            if code and code != "000" and not code.startswith("5"):
                self.learn_teach(port, t, code)
                return
            if final:
                self.stop_teach(port, "the terminal was closed before the service answered")
                return
            t["next"] = time.time() + 8
            self.save_teaching()
        except Exception as ex:
            logw(f"teach {port}: {type(ex).__name__}: {ex}", "ErrorMsg")
            if t:
                t["next"] = time.time() + 30
        finally:
            if t:
                t["busy"] = False

    def learn_teach(self, port: int, t: dict, code: str):
        """The service answers: learn the recipe (the command line of the process listening on the port) and the
        procedure (the commands the user typed), make sure the row is up, keep the case."""
        host, rport = t["host"], int(t["rport"])
        chat(f"🎓 {port}: it answers on {host} 127.0.0.1:{rport} (HTTP {code}) — learning how it was started…", "DiagnosticOk")
        d = core.discover(host, self.args.ssh)
        self.mem.discovered(host, d)
        svc = next((x for x in d.get("services") or [] if x["port"] == rport), None) or {}
        launch = svc.get("cmd") or (core.proc_cmdline(host, svc["pid"], bool(t.get("windows")), self.args.ssh) if svc.get("pid") else "")
        steps = [c for c in t.get("cmds") or [] if not self.TRIVIAL.match(c)]
        cds = [c for c in steps if re.match(r"^\s*cd\s+\S", c)]
        if launch and cds and not t.get("windows") and not re.match(r"^\s*(/|~)", launch):
            launch = f"{cds[-1].strip()} && {launch}"  # it was started from that folder
        if not launch and steps and not t.get("windows"):
            launch = " && ".join(steps)
        service = t.get("service") or svc.get("service") or f"port{rport}"
        health = "/health" if t.get("kind") == "llm" else "/"
        if launch:
            self.mem.add_recipe(host, service, rport, launch, health, "taught")
        self.mem.add_procedure(host, service, rport, steps, by="the user (teach terminal)", launch=launch)
        pr = core.probe(port, t.get("kind", ""), self.args.probe_timeout)
        self.mem.port_bringup(port, host, f"127.0.0.1:{rport}", service, ["the user in the teach terminal: " + " ; ".join(steps[-6:])],
                              pr.get("model", ""))
        self.mem.case("teach", host=host, ports=[port], service=service, request=f"bring up {t.get('notes')}", steps=steps[-12:],
                      fix=(f"recipe learnt: {clip(launch, 220)}" if launch else "procedure kept (no command line found)"),
                      outcome="learned", secs=round(time.time() - core.epoch(t["started"])))
        self.teaching.pop(port, None)
        self.save_teaching()
        chat(f"🎓 learnt {service} on {host} (port {rport}):\n   recipe: {clip(launch, 300) or '(none: the process command line was not readable)'}"
             + (f"\n   steps: " + " ; ".join(clip(x, 80) for x in steps[-8:]) if steps else "")
             + "\n   Next time I start it myself (detached). You can type exit in the teach terminal: if the service stops "
             "with it, I relaunch it from this recipe.", "DiagnosticOk")
        emit({"t": "alert", "text": f"ptrm learnt how to start {service} on {host}"})
        e = self.table.get(port)
        if e and e.host == host and not self.tun.scan().get(port):
            self.open_entry(e, how="bringup")
        self.wake.set()

    def stop_teach(self, port: int, why: str):
        t = self.teaching.pop(port, None)
        self.save_teaching()
        if not t:
            return
        steps = [c for c in t.get("cmds") or [] if not self.TRIVIAL.match(c)]
        self.mem.case("teach", host=t["host"], ports=[port], service=t.get("service"), request=f"bring up {t.get('notes')}",
                      steps=steps[-12:], outcome="abandoned", fix=why)
        chat(f"🎓 teach {port} closed: {why}" + (" (the commands are kept as a case)" if steps else ""), "Comment")

    # ----- tunnels (shared by the actions, the DASH keys and healing)
    def open_entry(self, e: Entry, how: str = "open") -> str:
        if e.auto_host and not e.host:
            pick = self.choose_host(e)
            if not pick:
                return f"Error: SSH_HOST of {e.lport} is empty and no known host can take it (discover a host, or name one in the table)"
            self.mem.assign(e.lport, pick[0], pick[1], pick[2])
            chat(f"   🧭 {e.lport}: SSH_HOST is empty: I chose {pick[0]} — {pick[2]}", "Comment")
            e = self.table.get(e.lport) or e
        with self.oplock:
            t = self.tun.scan().get(e.lport)
            if t:
                if (t["host"], int(t["rport"])) == (e.host, e.rport):
                    if t.get("managed"):
                        self.tun.set_want(e.lport, True)
                    return (f"already open: localhost:{e.lport} → {e.host} {e.remote} (ssh pid {t['pid']}"
                            + ("" if t.get("managed") else ", opened elsewhere") + ")")
                return (f"Error: port {e.lport} is held by a tunnel to {t['host']} {t['rip']}:{t['rport']} (ssh pid {t['pid']}); "
                        f"close or reopen it to switch to {e.host} {e.remote}")
            holders = core.local_listeners().get(e.lport)
            if holders:
                free = core.free_ports(self.listeners, max(1024, e.lport + 1), 4)
                return (f"Error: local port {e.lport} is taken by " + ", ".join(f"{h['name']} (pid {h['pid']})" for h in holders)
                        + f", not a tunnel. Free ports: {', '.join(map(str, free))}")
            self.opening.add(e.lport)
            self.emit_dash()
            try:
                r = self.tun.open(e.host, e.lport, e.rip, e.rport, wait=self.args.open_wait)
            finally:
                self.opening.discard(e.lport)
            self.mem.port_opened(e, r["ok"], r.get("err", ""), how=how)
            self.mem.touch_host(e.host, r["ok"], err=r.get("err", ""), via="tunnel")
        self.wake.set()
        if not r["ok"]:
            return f"Error: cannot open {e.lport} → {e.host} {e.remote}: {r.get('err')}"
        if e.disabled:
            self.tun.set_want(e.lport, False)  # opened by hand: not healed while the row is disabled
        pr = core.probe(e.lport, e.kind, self.args.probe_timeout)
        out = f"tunnel up: localhost:{e.lport} → {e.host} {e.remote} (ssh pid {r['pid']}, {r['ms']} ms); {self.describe_probe(pr)}"
        if pr["state"] == "dead":
            out += (f"\nthe tunnel works but nothing answers at {e.remote} on {e.host}: the service is down there "
                    "(discover the host; launch it from its recipe)")
        return out

    def close_entry(self, port: int, origin=None, asked: bool = False) -> str:
        with self.oplock:
            t = self.tun.scan().get(port)
            if not t:
                self.tun.set_want(port, False)
                self.heal.pop(port, None)
                return f"nothing open on {port} (and ptrm no longer keeps it open)"
            if not t.get("managed") and not asked:
                if origin and self.args.net_readonly:
                    return "System: denied — the network cannot close tunnels (--net-readonly)."
                if not self.approve(f"close the tunnel on {port} opened elsewhere (ssh pid {t['pid']})"):
                    return "System: denied by the user."
            r = self.tun.close(port, pid=t["pid"])
            self.heal.pop(port, None)
            self.mem.event("close", port=port, host=t["host"], remote=f"{t['rip']}:{t['rport']}", ok=r["ok"],
                           detail=r.get("err") or r.get("detail"))
        self.wake.set()
        return (f"closed {port} (ssh pid {t['pid']})" if r["ok"] else f"Error: {r.get('err')}")

    @staticmethod
    def describe_probe(pr: dict) -> str:
        st = pr.get("state")
        bits = [f"probe: {st}"]
        if pr.get("http"):
            bits.append(f"HTTP {pr['http']}")
        if pr.get("model"):
            bits.append(f"model {pr['model']}")
        if pr.get("ms") is not None and st not in ("closed", "dead"):
            bits.append(f"{pr['ms']} ms")
        if pr.get("banner"):
            bits.append(f"banner {pr['banner']!r}")
        if pr.get("detail"):
            bits.append(pr["detail"])
        return ", ".join(bits)

    def check(self, port: int, deep: bool = True) -> str:
        """Everything about one local port: the row, the probe, who listens, the tunnel and its ssh log, memory, and
        (deep) whether the host answers and whether the service answers on the host."""
        rows = self.fresh_rows(0)
        r = next((x for x in rows if x["port"] == port), None)
        e = self.table.get(port)
        out = []
        if r:
            out.append(core.row_line(r))
        else:
            out.append(f"{port}: not in the table ({self.table.path})")
        pr = core.probe(port, e.kind if e else "", self.args.probe_timeout)
        out.append(self.describe_probe(pr))
        holders = core.local_listeners().get(port) or []
        if holders:
            out.append("listening here: " + ", ".join(f"{h['name']} pid {h['pid']} on {h['addr']}" for h in holders))
        t = self.tun.scan().get(port)
        if t:
            out.append(f"tunnel: ssh pid {t['pid']} → {t['host']} {t['rip']}:{t['rport']}"
                       + (f", by ptrm since {when(t['since'])}" if t.get("managed") else ", opened elsewhere"))
        tail = self.tun.log_tail(port, 5)
        if tail and (not t or (r and r["state"] in ("down", "lost", "error"))):
            out.append("ssh log: " + tail.replace("\n", " | "))
        if port in self.heal:
            h = self.heal[port]
            out.append(f"healing: {h['tries']}/{self.args.heal_tries} tries" + (f", last: {clip(h['err'], 160)}" if h["err"] else ""))
        out.append("memory: " + self.mem.port_line(port))
        inc = self.incident_of(port)
        if inc:
            out.append("open incident:\n" + self.incident_brief(inc))
        elif e and deep and (not r or r["state"] in ("down", "lost", "off", "error", "stalled", "other")):
            out.append(self.diagnose_text(e.host, e.rport, e.kind, port))
            if e.rip not in ("127.0.0.1", "localhost"):
                out.append(f"(the row forwards to {e.rip}, not the host itself: the service check looked at 127.0.0.1)")
        out.append("host memory: " + (self.mem.host_line(e.host) if e else "-"))
        return "\n".join(out)

    def term_name(self, host: Optional[str]) -> str:
        """<user>@<host>: the name of a STATS terminal's buffer (one per host, so many hosts stay apart)."""
        host = (host or "").strip()
        if host.lower() in ("", "local", "localhost", "this", "here", socket.gethostname().lower()):
            import getpass
            return f"{getpass.getuser()}@{socket.gethostname()}"
        if "@" in host:
            return host
        try:
            user = pinv.ssh_config(host, self.args.ssh).get("user")
        except Exception:
            user = ""
        return f"{user}@{host}" if user else host

    def term_spec(self, host: str) -> dict:
        """A shell on the host (ssh -t: its login shell; Git Bash on serpenti) or on this machine, for the user to type
        commands in: a STATS pane named <user>@<host>, focused, in terminal mode."""
        local = host.lower() in ("local", "localhost", "this", "here", socket.gethostname().lower())
        if local:
            cmd = [os.environ.get("SHELL") or ("cmd.exe" if os.name == "nt" else "sh")] + ([] if os.name == "nt" else ["-l"])
        else:
            cmd = [self.args.ssh, "-t", host]
        return {"t": "stats", "key": f"{'local' if local else host}:shell", "host": None if local else host, "tool": "shell",
                "cmd": cmd, "title": f"shell · {self.term_name(host)}", "name": self.term_name(host), "focus": True,
                "insert": True, "dock": "dash"}  # below DASH, not in the STATS grid

    def host_monitor(self, host: str):
        """With g:ptrm_stats {'hosts'}: the first time ptrm reaches a host of the table, a monitor on it in STATS."""
        if not (self.stats_hosts and host) or host in self.stats_seen or host in self.off_hosts:
            return
        self.stats_seen.add(host)
        try:
            emit(self.stats_spec(host, None))
        except Exception as ex:
            logw(f"stats {host}: {ex}", "Comment")

    def stats_spec(self, host: Optional[str], tool: Optional[str]) -> dict:
        """The monitor terminal for STATS: which tool on which host, as a command line; "name": its buffer name."""
        spec = self._stats_spec(host, tool)
        spec["name"] = self.term_name(host)
        return spec

    def _stats_spec(self, host: Optional[str], tool: Optional[str]) -> dict:
        host = (host or "").strip()
        local = host.lower() in ("", "local", "localhost", "this", "here", socket.gethostname().lower())
        tool = (tool or "").strip()
        name = host or "local"
        if " " in tool:  # a monitor command line of your own: watch -n 2 nvidia-smi, iostat 2 …
            cmd = ["sh", "-c", tool] if local else [self.args.ssh, "-t", host, tool]
            spec = {"t": "stats", "key": f"{name}:{tool}", "host": host or None, "tool": tool.split()[0], "cmd": cmd,
                    "title": f"{clip(tool, 40)} · {name}"}
            if not local and self.mem.host(host).get("windows"):
                spec.update(cmd=[self.args.ssh, "-t", host], input=f"exec {tool}\n")
            return spec
        tool = tool.lower()
        if local:
            import platform
            have = {t: bool(shutil.which(t)) for t in ("macmon", "btop", "htop", "nvtop", "top", "nvidia-smi", "glances", "atop")}
            have["macmon"] = have["macmon"] and platform.machine() == "arm64"  # macmon: Apple Silicon only
            gpus = []
            windows = os.name == "nt"
            darwin = sys.platform == "darwin"
        else:
            d = self.mem.host(host)
            if not d.get("tools"):
                d = self.mem.discovered(host, core.discover(host, self.args.ssh))
            have = {t: True for t in (d.get("tools") or {})}
            gpus = d.get("gpu") or []
            windows = bool(d.get("windows"))
            darwin = (d.get("os") or {}).get("system") == "Darwin"
        nvidia = any("nvidia" in g.lower() for g in gpus) or (local and have.get("nvidia-smi"))
        if tool in ("gpu", "nvtop", "macmon", "nvidia-smi"):
            pick = next((t for t in ((["macmon"] if darwin else []) + ["nvtop"] + (["nvidia-smi"] if nvidia else []))
                         if have.get(t)), None)
            if tool != "gpu" and have.get(tool):
                pick = tool
            if not pick:
                raise ValueError(f"no GPU monitor on {host or 'this machine'} (tools: {' '.join(have) or 'unknown'})")
            tool = pick
        elif tool:
            if tool not in MONITORS:
                raise ValueError(f"{tool} is not a monitor ({', '.join(sorted(MONITORS))}; or give a whole command line)")
            if not have.get(tool) and not (tool == "top" and not windows):
                mons = [k for k, v in have.items() if v and k in MONITORS]
                raise ValueError(f"{tool} is not on {host or 'this machine'} (monitors there: {' '.join(mons) or 'none known'})")
        else:
            prefs = (["macmon"] if darwin and local else []) + ["btop", "htop"] + ([] if windows else ["top"])
            tool = next((t for t in prefs if have.get(t) or t == "top"), "top")
        line = {"top": "top -o cpu -s 2" if darwin else "top", "nvidia-smi": "nvidia-smi -l 2"}.get(tool, tool)
        if local:
            cmd = shlex.split(line)
        elif windows:  # Win32-OpenSSH drops the command of ssh -t: type it into the shell
            return {"t": "stats", "key": f"{name}:{tool}", "host": host, "tool": tool, "cmd": [self.args.ssh, "-t", host],
                    "input": f"exec {line}\n", "title": f"{tool} · {name}"}
        else:
            cmd = [self.args.ssh, "-t", host, line]
        return {"t": "stats", "key": f"{name}:{tool}", "host": host or None, "tool": tool, "cmd": cmd,
                "title": f"{tool} · {name}"}

    # ----- input
    def submit(self, text, origin=None, ctx=None):
        self.pending += 1
        self.tasks.put((text, origin, ctx))
        self.emit_state()

    def on_input(self, text: str, ctx):
        text = (text or "").strip()
        if not text:
            return
        if self.approval:
            self.approval["answer"] = text.lower()
            self.approval["event"].set()
            return
        if text in ("===a", ":auto"):
            self.auto = not self.auto
            chat(f"auto-approve (remote/local commands, launches, editor): {'ON' if self.auto else 'off'}", "WarningMsg")
            return self.emit_state()
        if text in ("===x", ":cancel"):
            return self.do_cancel()
        if text in ("===h", "?", ":help"):
            return chat(HELP_TEXT, "Comment")
        if text == "===r":
            self.wake.set()
            return chat("refreshing the table…", "Comment")
        if text == "====":
            self.recent.clear()
            return chat("context reset: earlier requests forgotten (memory kept)", "Comment")
        if text == "===H":
            self.args.heal = not self.args.heal
            chat(f"healing (reopen lost tunnels): {'on' if self.args.heal else 'OFF'}", "WarningMsg")
            return self.emit_state()
        if text == "===T":
            self.llm.think = not self.llm.think
            chat(f"LLM thinking (streamed into the LLM pane): {'on' if self.llm.think else 'OFF'}", "WarningMsg")
            return self.emit_state()
        if text.startswith("===off") or text.startswith("===on"):
            on = text.startswith("===off")
            arg = text[6 if on else 5:].strip()
            if arg and not arg.isdigit():  # a host
                return chat(("🚫 " if on else "▶ ") + self.set_host_disabled(arg, on, "===off" if on else ""), "Comment")
            if not arg.isdigit():
                dis = [e.lport for e in self.table.entries() if e.disabled]
                return chat(f"  disabled rows: {', '.join(map(str, dis)) or 'none'} · disabled hosts: "
                            f"{', '.join(sorted(self.off_hosts)) or 'none'} (===off <port|host> disables, ===on enables)", "Comment")
            return chat("  " + self.set_disabled(int(arg), on), "Comment")
        if text.startswith("===s"):
            q = text[4:].strip()
            hits = self.mem.recall(q, 10) if q else []
            return chat("\n".join(f"  {s:5.2f} {clip(d, 240)}" for s, d in hits) or "  (nothing remembered matches)", "Comment")
        if text.startswith("===n"):
            return self.net_command(text[4:])
        if text.startswith("===d"):
            chat("▶ " + text, "Title")
            self.hands_reply(text[4:].strip())
            return
        if text.startswith("===i"):
            return self.incident_command(text[4:])
        if text.startswith("===t"):
            arg = text[4:].strip()
            if arg.startswith("x") and arg[1:].strip().isdigit():
                return self.stop_teach(int(arg[1:].strip()), "closed by you")
            if arg.isdigit():
                return self.on_op({"op": "teach", "port": int(arg)})
            return chat("\n".join(f"  🎓 {p} on {t['host']} since {when(t['started'])}: {t.get('notes')} — {len(t.get('cmds') or [])} commands so far"
                                  for p, t in self.teaching.items()) or "  no teach terminal open (===t <port> opens one)", "Comment")
        if text.startswith("===b"):
            arg = text[4:].strip()
            ports = [int(arg)] if arg.isdigit() else [e.lport for e in self.table.entries() if not e.disabled]
            for p in ports:
                self.enqueue_bringup(p, auto=not arg.isdigit())
            return chat(f"  ⏫ bringing up {', '.join(map(str, ports))}", "Comment")
        waiting = [i for i in self.incidents.values() if i["state"] == "hands"]
        if waiting and (REPORT.search(text) or ACK.fullmatch(text)):
            chat("")
            chat("▶ " + text, "Title")
            self.hands_reply(text)
            if ACK.fullmatch(text) or not self.llm.ensure():
                return  # noted on the incident and rechecked; nothing for the LLM
            self.submit(text, None, ctx)
            return
        chat("")
        chat("▶ " + text, "Title")
        if self.busy:
            chat("  (queued after the current task)", "Comment")
        self.submit(text, None, ctx)

    def do_cancel(self):
        if self.busy:
            self.cancel.set()
            if self.approval:
                self.approval["answer"] = "n"
                self.approval["event"].set()
            chat("✖ cancelling…", "WarningMsg")

    def net_command(self, rest):
        sub, _, arg = rest.strip().partition(" ")
        if sub == "j":
            name, _, purpose = arg.strip().partition(" ")
            self.iface.join(name or None, purpose.strip() or None)
        elif sub == "l":
            self.iface.leave()
        elif sub == "s" and arg.strip():
            self.iface.say(arg.strip())
        elif sub == "w":
            chat(self.iface.net.who() if self.iface.connected else "not connected", "Comment")
        else:
            s = self.iface.summary()
            chat(f"🌐 {'joined' if s['joined'] else 'not joined'} · {'connected' if s['connected'] else 'disconnected'} "
                 f"· {s['network'] or 'network'} {s['hub']} as @{s['name']} [{s['state']}]\n"
                 "   ===nj [name] [purpose] join · ===nl leave · ===ns <text> say · ===nw who", "Comment")

    # ----- direct ops (DASH keys, :PtrmCheck …): no LLM, on their own thread
    def on_op(self, msg):
        self.ops.put(msg)

    def op_worker(self):
        while self.running:
            try:
                msg = self.ops.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.run_op(msg)
            except Exception as e:
                chat(f"  ✖ {msg.get('op')}: {type(e).__name__}: {e}", "ErrorMsg")

    def run_op(self, msg):
        op = msg.get("op")
        port = as_port(msg.get("port"))
        host = str(msg.get("host") or "").strip() or None
        if op == "refresh":
            self.wake.set()
            return
        if op == "disable_host":
            if not host and port:
                e = self.table.get(port)
                host = e.host if e else None
            if not host:
                return chat("  ✖ disable host: no host on this line", "ErrorMsg")
            on = (host not in self.off_hosts) if msg.get("on") is None else bool(msg.get("on"))
            res = self.set_host_disabled(host, on, "Xh in DASH" if on else "")
            return chat(("🚫 " if on else "▶ ") + res, "WarningMsg" if on else "Title")
        if op == "disable":
            e = self.table.get(port) if port else None
            if not e:
                return chat("  ✖ disable: no row on this line", "ErrorMsg")
            on = (not e.disabled) if msg.get("on") is None else bool(msg.get("on"))
            return chat("  " + self.set_disabled(port, on), "Comment")
        if op == "stats" and host == "hosts":  # a monitor on each host of the table, once ptrm reaches it
            self.stats_hosts = True
            for r in list(self.rows):  # the ones reached already
                if r.get("tunnel") and r["state"] in ("up", "loading", "down", "stalled") and not r.get("disabled"):
                    self.host_monitor(r["host"])
            return
        if op == "term":  # t in DASH: a shell on the host in STATS, to type commands there
            if not host and port:
                e = self.table.get(port)
                host = e.host if e else None
            if not host:
                return chat("  ✖ terminal: no host on this line (SSH_HOST empty and none chosen yet)", "ErrorMsg")
            spec = self.term_spec(host)
            emit(spec)
            return chat(f"» terminal: {spec['title']}", "Comment")
        if op == "stats":
            try:
                spec = self.stats_spec(host, msg.get("tool"))
            except ValueError as e:
                return chat(f"  ✖ stats: {e}", "ErrorMsg")
            emit(spec)
            return chat(f"» stats: {spec['title']}", "Comment")
        if op == "incident":
            iid = msg.get("incident")
            return self.incident_command(str(iid or ""))
        if op == "stats_closed":
            key = str(msg.get("key") or "")
            if key.startswith("teach:") and key[6:].isdigit() and int(key[6:]) in self.teaching:
                threading.Thread(target=self._teach_poll, args=(int(key[6:]), True), daemon=True).start()
            return
        if op in ("teach", "bringup"):
            e = self.table.get(port) if port else None
            if not e:
                return chat(f"  ✖ {op}: no row on this line", "ErrorMsg")
            if op == "bringup":
                return self.enqueue_bringup(port)
            if not e.host:
                pick = self.choose_host(e)
                if not pick:
                    return chat("  ✖ teach: SSH_HOST is empty and no known host answers", "ErrorMsg")
                self.mem.assign(port, pick[0], pick[1], pick[2])
                e = self.table.get(port)
            return self.start_teach(e, "you asked (T)", self.teach_hints(e))
        if op == "diagnose":
            inc = self.incident_of(port) if port else self.incident_of(host=host)
            if inc:
                chat(f"» incident #{inc['id']}: diagnosing again…", "Comment")
                inc["next_check"] = 0
                for p in inc["ports"]:
                    self.heal.pop(int(p), None)
                return self.enqueue_incident(inc)
            e = self.table.get(port) if port else None
            host = host or (e.host if e else None)
            if not host:
                return chat("  ✖ no host on this line", "ErrorMsg")
            chat(f"» diagnose {host}" + (f" (port {port} → {e.rport})" if e else "") + " …", "Comment")
            return chat(indent(self.diagnose_text(host, e.rport if e else None, e.kind if e else "", port if e else None)), "Comment")
        if op in ("discover", "reach") and not host and port:
            e = self.table.get(port)
            host = e.host if e else None
        if op in ("discover", "reach"):
            if not host:
                return chat("  ✖ no host on this line", "ErrorMsg")
            chat(f"» {op} {host} …", "Comment")
            if op == "reach":
                r = core.reach(host, self.args.ssh)
                self.mem.touch_host(host, r["ok"], r["ms"], r["err"], via="reach")
                chat(f"  {'✔ reachable' if r['ok'] else '✖ not reachable'} {host}: "
                     + (f"{r['ms']} ms, {r['os']}" if r["ok"] else r["err"]), "DiagnosticOk" if r["ok"] else "ErrorMsg")
            else:
                d = self.mem.discovered(host, core.discover(host, self.args.ssh))
                chat("  " + self.mem.host_line(host, d), "Comment")
                for c in d.get("_new_changes") or []:
                    chat(f"  {'⚠' if c['sev'] else '·'} changed: {c['what']}", "WarningMsg" if c["sev"] else "Comment")
                for x in d.get("risks") or []:
                    if x.get("sev"):
                        chat(f"  ⚠ risk: {x['text']}" + (f" (fix: {x['fix']})" if x.get("fix") else ""), "WarningMsg")
            return self.emit_dash()
        if op == "history":
            if port:
                return chat("» history " + str(port) + "\n" + indent(self.mem.history(port=port, limit=15)), "Comment")
            if host:
                return chat("» history " + host + "\n" + indent(self.mem.history(host=host, limit=15)), "Comment")
            return
        if not port:
            return chat(f"  ✖ {op}: no port on this line", "ErrorMsg")
        if op == "check":
            chat(f"» check {port} …", "Comment")
            return chat(indent(self.check(port)), "Comment")
        e = self.table.get(port)
        if op in ("open", "reopen") and not e:
            return chat(f"  ✖ {port} is not in the table ({self.table.path})", "ErrorMsg")
        if op == "open":
            chat(f"» open {port} → {e.host} {e.remote} …", "Comment")
            out = self.open_entry(e)
        elif op == "close":
            out = self.close_entry(port, asked=True)
        elif op == "reopen":
            chat(f"» reopen {port} …", "Comment")
            c = self.close_entry(port, asked=True)
            time.sleep(0.3)
            out = c + "\n" + self.open_entry(e, how="reopen")
        else:
            return chat(f"  ✖ unknown op {op}", "ErrorMsg")
        chat(indent(out), "ErrorMsg" if "Error" in out else "DiagnosticOk")

    # ----- editor requests
    def nvim(self, op, timeout=None, **params) -> dict:
        with self.rlock:
            self.req_id += 1
            rid = self.req_id
            ev = threading.Event()
            self.reqs[rid] = {"ev": ev, "res": None}
        emit(dict({"t": "req", "id": rid, "op": op}, **params))
        end = time.time() + (timeout or self.args.cmd_timeout)
        try:
            while time.time() < end:
                if ev.wait(0.2):
                    return self.reqs[rid]["res"] or {"ok": False, "err": "no result"}
                if self.cancel.is_set():
                    raise Cancelled()
            return {"ok": False, "err": "Neovim did not answer in time"}
        finally:
            with self.rlock:
                self.reqs.pop(rid, None)

    def on_result(self, msg):
        with self.rlock:
            r = self.reqs.get(msg.get("id"))
        if r:
            r["res"] = msg
            r["ev"].set()

    def approve(self, what: str) -> bool:
        if self.auto:
            return True
        ev = threading.Event()
        self.approval = {"text": what, "event": ev, "answer": ""}
        limit = self.approve_timeout
        chat(f"  ❓ allow {what} ?  [y]es / [n]o / [a]lways" + (f"   (no answer in {limit // 60} min: no)" if limit else ""), "WarningMsg")
        emit({"t": "show"})
        if limit:
            emit({"t": "alert", "text": f"ptrm asks: allow {clip(what, 120)} ?"})
        self.set_phase("approval")
        end = time.time() + limit if limit else None
        while not ev.wait(0.2):
            if self.cancel.is_set():
                break
            if end and time.time() > end:
                self.approval["answer"] = "n"
                break
        ans = self.approval["answer"]
        self.approval = None
        if ans.startswith("a"):
            self.auto = True
        ok = ans[:1] in ("y", "a")
        chat("    approved" if ok else "    denied", "Comment")
        self.set_phase("working")
        return ok

    # ----- worker
    def worker(self):
        while self.running:
            try:
                text, origin, ctx = self.tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            if text == "\x00incident":
                self.run_incident(origin)
                continue
            if text == "\x00bringup":
                self.run_bringup(origin, bool((ctx or {}).get("auto")))
                continue
            self.pending = max(0, self.pending - 1)
            self.current = (text, origin)
            self.cancel.clear()
            self.set_phase("working")
            if origin:
                chat("")
                chat(f"🌐 network request #{origin.get('id')} from @{origin.get('from')}: {clip(text, 300)}", "Special")
                logw(f"▶ 🌐 #{origin.get('id')} @{origin.get('from')}: {clip(text, 200)}", "Title")
            else:
                logw(f"▶ {clip(text, 200)}", "Title")
            status, reply = "FAILED", ""
            try:
                status, reply = self.run_task(text, origin, ctx)
            except Cancelled:
                status, reply = "CANCELLED", "Cancelled."
            except Exception as e:
                status, reply = "FAILED", f"{type(e).__name__}: {e}"
                chat(f"❌ {reply}", "ErrorMsg")
            hl = "DiagnosticOk" if status == "SUCCESS" else "ErrorMsg"
            chat(f"{'✔' if status == 'SUCCESS' else '✖'} {status}", hl)
            if reply:
                chat(reply)
            if status == "SUCCESS" and not origin:
                self.recent.append(f"- {clip(text, 160)} → {clip(reply, 200)}")
            if origin:
                self.iface.answer(origin, status, reply)
            self.current = None
            self.set_phase("idle")
            if self.iface.connected:
                self.iface.set_state(self.iface.idle_state())

    def context_block(self, text, origin, ctx) -> str:
        rows = self.fresh_rows(self.args.interval)
        parts = [f"[TODAY] {utc_now()} (UTC)",
                 f"[THIS MACHINE] {socket.gethostname()} · the table: {self.table.path}",
                 "[TABLE] port · state · host · remote · notes  [live details]\n"
                 + ("\n".join(core.row_line(r) for r in rows) or "(no rows)")]
        hosts = []
        for r in rows:
            if r["host"] not in hosts:
                hosts.append(r["host"])
        for h in self.mem.known_hosts():
            if h not in hosts:
                hosts.append(h)
        if self.incidents:
            parts.append("[INCIDENTS] open; ptrm diagnoses and rechecks them by itself\n"
                         + "\n".join(self.incident_brief(i) for i in list(self.incidents.values())[:4]))
        table_hosts = {r["host"] for r in rows}
        hl = [clip(self.mem.host_line(h, full=h in table_hosts or h in text), 1100) if i < 6 else clip(self.mem.host_line(h, full=False), 300)
              for i, h in enumerate(hosts[:12])]
        parts.append("[HOSTS] what ptrm remembers of each host\n" + "\n".join(hl))
        hits = self.mem.recall(text, 6)
        if hits:
            parts.append("[RECALL] memory matching the request\n" + "\n".join(f"- {clip(d, 300)}" for _, d in hits))
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network ({'private' if origin.get('to') else 'in the room'})."
                         + (" Changes from the network are NOT allowed (read-only)." if self.args.net_readonly else ""))
        else:
            if ctx is None:
                r = self.nvim("context", timeout=5)
                ctx = r.get("out") if r.get("ok") else None
            if isinstance(ctx, dict) and (ctx.get("line") or ctx.get("port") or ctx.get("dash_port")):
                pos = f"[USER POSITION] buffer {ctx.get('buffer') or '?'}\n  line: {clip(ctx.get('line'), 200)}"
                for key, what in (("port", "the table row under the cursor in the user's buffer"),
                                  ("dash_port", "the row selected in DASH")):
                    p = as_port(ctx.get(key))
                    if p:
                        row = next((x for x in rows if x["port"] == p), None)
                        pos += f"\n  {what} (\"this\", \"here\"): " + (core.row_line(row) if row else str(p))
                parts.append(pos)
            if self.recent:
                parts.append("[EARLIER SUCCESSFUL REQUESTS]\n" + "\n".join(self.recent))
        if self.iface.connected:
            parts.append(f"[NETWORK] {self.iface.net.network} as @{self.iface.net.name}; members: {self.iface.net.who()}")
        else:
            parts.append("[NETWORK] not joined (network actions unavailable)")
        return "\n\n".join(parts)

    def run_task(self, text, origin, ctx):
        self.trail = []
        t0 = time.time()
        try:
            status, reply = self._run_task(text, origin, ctx)
        except Cancelled:
            status, reply = "CANCELLED", "Cancelled."
        self.record_request(text, origin, status, reply, time.time() - t0)
        if status == "CANCELLED":
            raise Cancelled()
        return status, reply

    def record_request(self, text, origin, status, reply, secs):
        """A request that changed or troubleshot something becomes a case; a port it brought up keeps the steps."""
        acted = [t for t in self.trail if t[0] in ("open", "reopen", "close", "set_entry", "remove_entry", "launch", "stop",
                                                    "remote", "diagnose", "check", "hands", "runbook", "docker")]
        if not acted:
            return
        steps = [f"{k} {lbl}".strip() + f" → {clip(res, 90)}" for k, lbl, res in self.trail if k not in ("recall", "history", "status")]
        ports = sorted({p for _, _, _, p in [(k, l, r, as_port(re.search(r"port=(\d+)", l).group(1)) if re.search(r"port=(\d+)", l) else None)
                                              for k, l, r in self.trail] if p})
        hosts = [h for h in dict.fromkeys(re.findall(r"host=(\S+)", " ".join(l for _, l, _ in self.trail)))]
        host = hosts[0] if hosts else (self.table.get(ports[0]).host if ports and self.table.get(ports[0]) else "")
        self.mem.case("request", host=host, ports=ports, request=clip(text, 300), steps=steps[-10:],
                      outcome="ok" if status == "SUCCESS" else status.lower(), fix=clip(reply, 300), secs=round(secs),
                      by=(f"@{origin.get('from')}" if origin else "user"))
        if status != "SUCCESS":
            return
        rows = self.fresh_rows(2)
        for p in ports:
            r = next((x for x in rows if x["port"] == p), None)
            e = self.table.get(p)
            if r and e and r["state"] in ("up", "loading") and any(k in ("launch", "open", "reopen", "set_entry") for k, _, _ in self.trail):
                self.mem.port_bringup(p, e.host, e.remote, e.service, steps[-8:], (r.get("probe") or {}).get("model") or "")

    def _run_task(self, text, origin, ctx):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self.context_block(text, origin, ctx) + f"\n\n[REQUEST]\n{text}"}]
        for turn in range(1, self.args.max_turns + 1):
            if self.cancel.is_set():
                raise Cancelled()
            self.set_phase(f"thinking {turn}/{self.args.max_turns}")
            logw(f"── turn {turn}/{self.args.max_turns} ──", "Comment")
            self.trim(messages)
            raw = self.llm.stream(messages, self.cancel)
            if self.cancel.is_set():
                raise Cancelled()
            act = parse_llm_json(raw)
            messages.append({"role": "assistant", "content": raw})
            if not act or not (act.get("action_type") or act.get("action")):
                messages.append({"role": "user", "content": "System: reply with ONE JSON object with \"action_type\"."})
                chat("  (unparsable LLM reply, asking again)", "Comment")
                continue
            act = normalize_action(act)
            kind = str(act.get("action_type") or "").strip()
            if kind == "declare_result":
                st = str(act.get("status") or "SUCCESS").upper()
                return ("SUCCESS" if st == "SUCCESS" else "FAILED"), str(act.get("reply") or "")
            label = " ".join(f"{k}={clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 60)}"
                             for k, v in act.items() if k not in ("action_type", "reason"))
            chat(f"  ⚙ {kind} {label}", "Function")
            if act.get("reason"):
                chat(f"    {clip(act['reason'], 200)}", "Comment")
            self.set_phase(kind)
            try:
                evidence = self.do_action(kind, act, origin)
            except Cancelled:
                raise
            except Exception as e:
                evidence = f"Error: {type(e).__name__}: {e}"
            evidence = str(evidence)
            first = evidence.split("\n", 1)[0]
            self.trail.append((kind, label, first))
            chat(f"    → {clip(first, 240)}" + (f"  (+{evidence.count(chr(10))} lines)" if "\n" in evidence else ""),
                 "ErrorMsg" if evidence.startswith(("Error", "System: denied")) else "Comment")
            messages.append({"role": "user", "content": f"[RESULT of {kind}]\n{evidence[:self.args.max_output]}"})
        return "FAILED", f"Stopped after {self.args.max_turns} turns without a result."

    def trim(self, messages):
        """Keep the conversation inside the small model's context: old results are cut short first."""
        budget = self.args.max_history
        total = sum(len(m["content"]) for m in messages)
        i = 2
        while total > budget and i < len(messages) - 2:
            m = messages[i]
            if m["role"] == "user" and len(m["content"]) > 400:
                total -= len(m["content"]) - 400
                m["content"] = m["content"][:400] + " …(older result cut)"
            i += 1

    # ----- actions
    def host_arg(self, a) -> str:
        h = str(a.get("host") or "").strip()
        if not h and as_port(a.get("port")):
            e = self.table.get(as_port(a.get("port")))
            h = e.host if e else ""
        return h

    def do_action(self, kind, a, origin):
        ro = bool(origin and self.args.net_readonly)
        changes = {"open", "close", "reopen", "set_entry", "remove_entry", "remote", "launch", "stop", "local", "note",
                   "hands", "incident", "runbook", "teach", "bringup"}
        if ro and kind in changes:
            return "System: denied — changes from network requests are disabled (--net-readonly)."
        port = as_port(a.get("port"))

        if kind == "status":
            rows = self.fresh_rows(0)
            return "\n".join(core.row_line(r) for r in rows) or "(the table has no rows)"
        if kind == "check":
            if not port:
                return "Error: check needs \"port\""
            return self.check(port)
        if kind == "local_ports":
            lo = as_port(a.get("from")) or 8000
            hi = as_port(a.get("to")) or lo + 999
            L = core.local_listeners()
            used = [f"{p} {', '.join(x['name'] for x in L[p][:2])}" for p in sorted(L) if lo <= p <= hi]
            free = core.free_ports(L, lo, 6, avoid={e.lport for e in self.table.entries()})
            return (f"listening here in {lo}-{hi}: " + ("; ".join(used) or "nothing")
                    + f"\nfree (and not in the table): {', '.join(map(str, free))}")
        if kind in ("open", "reopen"):
            if not port:
                return f"Error: {kind} needs \"port\" (a row of the table; add one with set_entry)"
            e = self.table.get(port)
            if not e:
                return f"Error: {port} is not in the table; set_entry it first"
            if kind == "reopen":
                c = self.close_entry(port, origin)
                if c.startswith("System"):
                    return c
                time.sleep(0.3)
                return c + "\n" + self.open_entry(e, how="reopen")
            return self.open_entry(e)
        if kind == "close":
            if not port:
                return "Error: close needs \"port\""
            return self.close_entry(port, origin)
        if kind == "set_entry":
            if not port:
                return "Error: set_entry needs \"port\" (the local port)"
            old = self.table.get(port)
            host = str(a.get("host") if a.get("host") is not None else ("" if not old or old.auto_host else old.host)).strip()
            rport = as_port(a.get("remote_port")) or (old.rport if old and not old.auto_rport else None)
            notes = str(a["notes"]).strip() if a.get("notes") is not None else (old.notes if old else "")
            e = Entry(host, port, str(a.get("remote_ip") or (old.rip if old and not old.auto_rip else "")).strip(), rport or port,
                      notes.replace("|", "/"), auto_host=not host, auto_rip=not (a.get("remote_ip") or (old and not old.auto_rip)),
                      auto_rport=not rport,
                      disabled=bool(a["disabled"]) if a.get("disabled") is not None else bool(old and old.disabled))
            if old and old.auto_host and host:
                self.mem.assign(port, None)
            self.table.upsert(e)
            self.mem.event("table", port=port, host=host, remote=e.remote, service=e.service,
                           detail=("changed: " + " | ".join(old.cells()) if old else "added"))
            emit({"t": "req", "id": 0, "op": "reload", "path": self.table.path})
            self.wake.set()
            t = self.tun.scan().get(port)
            note = ""
            if t and (t["host"], int(t["rport"])) != (e.host, e.rport):
                note = f"\nthe live tunnel still goes to {t['host']} {t['rip']}:{t['rport']}: reopen {port} to apply"
            return ("changed" if old else "added") + f" row: {' | '.join(e.cells())}" + note
        if kind in ("disable_host", "enable_host"):
            host = self.host_arg(a)
            if not host:
                return f"Error: {kind} needs \"host\""
            return self.set_host_disabled(host, kind == "disable_host", str(a.get("reason") or "asked"))
        if kind in ("disable", "enable"):
            if not port:
                return f"Error: {kind} needs \"port\""
            return self.set_disabled(port, kind == "disable")
        if kind == "remove_entry":
            if not port:
                return "Error: remove_entry needs \"port\""
            old = self.table.remove(port)
            if not old:
                return f"Error: no row for {port}"
            self.mem.event("table", port=port, host=old.host, remote=old.remote, service=old.service,
                           detail="removed: " + " | ".join(old.cells()))
            emit({"t": "req", "id": 0, "op": "reload", "path": self.table.path})
            self.wake.set()
            t = self.tun.scan().get(port)
            return f"removed row {' | '.join(old.cells())}" + (f" (its tunnel, ssh pid {t['pid']}, is still open)" if t else "")
        if kind == "reach":
            host = self.host_arg(a)
            if not host:
                return "Error: reach needs \"host\""
            r = core.reach(host, self.args.ssh)
            self.mem.touch_host(host, r["ok"], r["ms"], r["err"], via="reach")
            return f"{host}: " + (f"reachable over ssh in {r['ms']} ms ({r['os']})" if r["ok"] else f"NOT reachable: {r['err']}")
        if kind == "discover":
            host = self.host_arg(a)
            if not host:
                return "Error: discover needs \"host\""
            d = core.discover(host, self.args.ssh)
            h = self.mem.discovered(host, d)
            self.emit_dash()
            if not d.get("ok"):
                return f"Error: {host} not reachable: {d.get('err')} (diagnose tells which layer)"
            svc = "\n".join(f"  {s['port']} {s['service']} — {s['process']} pid {s['pid']} on {s['bind']}"
                            + (f"\n     {clip(s['cmd'], 300)}" if s.get("cmd") else "") for s in d["services"])
            dk = "\n".join(f"  docker {c['name']} {c['image']} {c['ports']}" for c in d.get("docker") or [])
            ch = "\n".join(f"  {'⚠ ' if c['sev'] else ''}{c['what']}" for c in h.get("_new_changes") or [])
            rk = "\n".join(f"  {'⚠ ' if r['sev'] else ''}{r['text']}" + (f" (fix: {r['fix']})" if r.get("fix") else "") for r in h.get("risks") or [])
            return (self.mem.host_line(host, h) + "\nlistening services:\n" + (svc or "  none") + ("\n" + dk if dk else "")
                    + (f"\nchanged since the last look:\n{ch}" if ch else "") + (f"\nrisks:\n{rk}" if rk else ""))
        if kind == "diagnose":
            e = self.table.get(port) if port else None
            host = str(a.get("host") or "").strip() or (e.host if e else "")
            if not host:
                return "Error: diagnose needs \"port\" (a row) or \"host\""
            inc = self.incident_of(port) if port else self.incident_of(host=host)
            rport = as_port(a.get("remote_port")) or (e.rport if e and e.host == host else None)
            txt = self.diagnose_text(host, rport, e.kind if e else "", port if e else None)
            return (f"open incident:\n{self.incident_brief(inc)}\n" if inc else "") + txt
        if kind == "hands":
            host = self.host_arg(a)
            raw = a.get("steps") or a.get("text") or []
            steps = []
            for x in (raw if isinstance(raw, list) else [raw]):
                for line in re.split(r"\n+|;\s+(?=\d+[.)]\s)", str(x)):
                    line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip()
                    if line:
                        steps.append(line)
            if not host or not steps:
                return "Error: hands needs \"host\" and \"steps\" (a list of physical steps)"
            if origin:
                return "System: denied — only the local user is asked for remote hands."
            inc = self.incident_of(host=host)
            if not inc:
                rows = self.fresh_rows(0)
                bad = [r for r in rows if r["host"] == host and r["state"] in INC_BAD + ("off",)]
                for r in bad:  # wanted from now on: healing opens it when the host is back, and that resolves it
                    if r["state"] == "off":
                        self.tun.set_want(r["port"], True, self.table.get(r["port"]))
                        r["state"], r["want"] = "lost", True
                        self.good_state.setdefault(r["port"], "wanted")
                if bad:
                    self.open_incident(bad[0])
                    inc = self.incident_of(host=host)
                    inc["queued"] = True  # this task handles it; the watcher rechecks it later
                    for r in bad[1:]:
                        inc["ports"][str(r["port"])] = {"state": r["state"], "was": "wanted", "since": utc_now(),
                                                        "remote": f"{r['remote_ip']}:{r['remote_port']}", "service": r.get("service") or ""}
            why = str(a.get("why") or a.get("reason") or "")
            if inc:
                dg = inc.get("diag") or {"layer": "asked", "cause": why or "ptrm asked for help", "hands": []}
                if why and not inc.get("diag"):
                    inc["diag"] = dg
                inc["state"] = "asking"
                inc["queued"] = False
                inc["next_check"] = time.time() + 20
                self.request_hands(inc, dg, changed=True, extra=steps)
                inc["asked_at"] = time.time()
                return (f"asked the user (incident #{inc['id']}): " + " | ".join(steps) + ". The steps are NOT done yet: the user "
                        "answers later in the chat and ptrm rechecks by itself (every 20 s). Do not check again now: finish with "
                        "declare_result (what you found, what you asked the user to do).")
            chat(f"🖐 {host}: {why}\n" + "\n".join(f"    {i}. {x}" for i, x in enumerate(steps, 1)), "WarningMsg")
            emit({"t": "alert", "text": f"remote hands: {host} — {clip(why or steps[0], 120)}"})
            self.mem.event("hands", host=host, detail=clip(why + ": " + " | ".join(steps), 300))
            return "asked the user: " + " | ".join(steps) + ". The steps are NOT done yet: finish with declare_result."
        if kind == "incident":
            iid = as_port(a.get("id")) or (int(a["id"]) if str(a.get("id") or "").isdigit() else None)
            inc = self.incidents.get(iid) if iid else (self.incident_of(port) if port else next(iter(self.incidents.values()), None))
            if not inc:
                return "no open incident" + (f" #{iid}" if iid else "") + " (resolved ones are cases: recall/history)"
            if a.get("close"):
                self.dismiss_incident(inc)
                return f"incident #{inc['id']} closed: {a['close']} (its ports are no longer kept open)"
            human = str(a.get("human") or a.get("text") or "").strip()
            if human and not any(x["text"] == human for x in inc.get("human") or []):
                self.hands_reply(human, inc)
            return self.incident_brief(inc)
        if kind in ("bringup", "teach"):
            e = self.table.get(port) if port else None
            if not e:
                return f"Error: {kind} needs \"port\" (a row of the table; set_entry it first)"
            if origin and kind == "teach":
                return "System: denied — only the local user can be asked to teach."
            if kind == "bringup":
                self.bringing.add(port)
                try:
                    res = self.bring_up(port)
                finally:
                    self.bringing.discard(port)
                return f"bring-up of {port}: {res}" + ("" if res in ("up", "teaching") else " (see the PTRM pane for the details)")
            if not e.host:
                pick = self.choose_host(e)
                if not pick:
                    return "Error: SSH_HOST is empty and no known host answers"
                self.mem.assign(port, pick[0], pick[1], pick[2])
                e = self.table.get(port)
            res = self.start_teach(e, str(a.get("why") or a.get("reason") or "asked"), self.teach_hints(e))
            return (f"teach terminal for {port} on {e.host}: {res}. The user brings the service up there; ptrm learns by itself. "
                    "Finish with declare_result.")
        if kind == "runbook":
            host = self.host_arg(a)
            step = str(a.get("step") or a.get("text") or "").strip()
            if not host:
                return "Error: runbook needs \"host\""
            if not step:
                return "runbook of " + host + ":\n" + ("\n".join(f"- {r.get('step')} (used {r.get('used')}×)" for r in self.mem.host(host).get("runbook") or []) or "(empty)")
            return self.mem.runbook_add(host, step, "ptrm")
        if kind == "remote":
            host, cmd = self.host_arg(a), str(a.get("command") or "").strip()
            if not host or not cmd:
                return "Error: remote needs \"host\" and \"command\""
            if not self.approve(f"run on {host}: {clip(cmd, 200)}"):
                return "System: denied by the user."
            prep = core.WINPREP if self.mem.host(host).get("windows") else ""  # MSYS: /FI stays /FI
            r = core.ssh_run(host, prep + cmd + "\n", self.args.remote_timeout, self.args.ssh)
            self.mem.touch_host(host, r["rc"] != 255, err=r["err"] if r["rc"] == 255 else "", via="remote")
            self.mem.event("remote", host=host, ok=r["rc"] == 0, detail=f"$ {clip(cmd, 160)} → exit {r['rc']}")
            if r["rc"] == 255:
                return f"Error: ssh failed: {core.ssh_error(r['err'], r['rc'])}"
            return f"exit {r['rc']}\n{r['out'][-3000:]}" + (f"\nstderr: {r['err'][-800:]}" if r["err"] else "")
        if kind == "launch":
            return self.launch(a, origin)
        if kind == "stop":
            host = self.host_arg(a)
            if not host:
                return "Error: stop needs \"host\""
            pid = as_port(a.get("pid")) if a.get("pid") else None
            d = self.mem.host(host)
            if not pid:
                rport = as_port(a.get("remote_port")) or port
                if not rport:
                    return "Error: stop needs \"port\" (the remote port) or \"pid\""
                dd = core.discover(host, self.args.ssh)
                d = self.mem.discovered(host, dd)
                s = next((s for s in dd.get("services") or [] if s["port"] == rport), None)
                if not s or not s.get("pid"):
                    return f"nothing listens on {rport} on {host} (nothing to stop)"
                pid = s["pid"]
                what = f"{s['process']} pid {pid} ({s['service']} on {rport})"
            else:
                what = f"pid {pid}"
            if not self.approve(f"stop {what} on {host}"):
                return "System: denied by the user."
            r = core.stop(host, pid, bool(d.get("windows")), self.args.ssh)
            self.mem.event("stop", host=host, ok=r["ok"], detail=f"{what}: {r['out']}")
            self.wake.set()
            return ("stopped " if r["ok"] else "Error: could not stop ") + f"{what}: {r['out']}"
        if kind == "logs":
            host, rport = self.host_arg(a), as_port(a.get("remote_port")) or port
            if not host or not rport:
                return "Error: logs needs \"host\" and \"port\" (the remote port)"
            return core.remote_log(host, rport, 40, self.args.ssh) or "(no log: ptrm did not launch it, or it wrote nothing)"
        if kind == "docker":
            host = self.host_arg(a)
            args = a.get("args") or "ps"
            argv = args if isinstance(args, list) else shlex.split(str(args))
            if not host or not argv:
                return "Error: docker needs \"host\" and \"args\""
            if argv[0] not in DOCKER_READONLY:
                if ro:
                    return "System: denied — read-only network request."
                if not self.approve(f"docker on {host}: {' '.join(argv)}"):
                    return "System: denied by the user."
            rc, out, err = core.run(["docker", "-H", f"ssh://{host}", *argv], self.args.remote_timeout)
            self.mem.event("remote", host=host, ok=rc == 0, detail=f"docker {' '.join(argv)[:120]} → exit {rc}")
            return f"exit {rc}\n{out[-3000:]}" + (f"\nstderr: {err[-800:]}" if err.strip() else "")
        if kind == "recall":
            q = str(a.get("query") or a.get("text") or "")
            hits = self.mem.recall(q, 10)
            return "\n".join(f"{s:5.2f} {clip(d, 300)}" for s, d in hits) or "nothing remembered matches"
        if kind == "history":
            if port:
                return self.mem.history(port=port, limit=20)
            host = str(a.get("host") or "").strip()
            return self.mem.history(host=host, limit=20) if host else "Error: history needs \"port\" or \"host\""
        if kind == "note":
            text = str(a.get("text") or "").strip()
            target = port or str(a.get("host") or "").strip()
            if not text or not target:
                return "Error: note needs \"text\" and \"port\" or \"host\""
            return self.mem.note(target, text)
        if kind == "stats":
            try:
                spec = self.stats_spec(a.get("host"), a.get("tool"))
            except ValueError as e:
                return f"Error: {e}"
            if origin:
                return "System: denied — network requests cannot open terminals on the user's screen."
            emit(spec)
            return f"STATS pane opened: {spec['title']} ({' '.join(spec['cmd'])})"
        if kind == "local":
            cmd = str(a.get("command") or "").strip()
            if not cmd:
                return "Error: local needs \"command\""
            if origin:
                return "System: denied — network requests cannot run commands on this machine."
            if not self.approve(f"run here: {clip(cmd, 200)}"):
                return "System: denied by the user."
            rc, out, err = core.run(["sh", "-c", cmd], self.args.remote_timeout)
            return f"exit {rc}\n{out[-3000:]}" + (f"\nstderr: {err[-800:]}" if err.strip() else "")
        if kind in ("run_command", "run_lua"):
            if origin:
                return "System: denied — network requests cannot drive the user's editor."
            if kind == "run_command":
                cmd = str(a.get("command") or "").strip().lstrip(":")
                if not cmd:
                    return "Error: run_command needs \"command\""
                if not self.approve(f":{cmd}"):
                    return "System: denied by the user."
                r = self.nvim("ex", command=cmd)
            else:
                code = str(a.get("code") or "")
                if not code.strip():
                    return "Error: run_lua needs \"code\""
                if not self.approve("Lua: " + clip(code, 200)):
                    return "System: denied by the user."
                r = self.nvim("lua", code=code)
            return (str(r.get("out") or "(ok, no output)") if r.get("ok") else f"Error: {r.get('err')}")
        if kind == "who":
            return self.iface.net.who() if self.iface.connected else "System: not on the network."
        if kind == "ask_agent":
            text = str(a.get("text") or "").strip()
            if not text:
                return "Error: ask_agent needs \"text\""
            self.set_phase("waiting @" + str(a.get("to")))
            return self.iface.ask(str(a.get("to") or ""), text, a.get("wait") or self.args.ask_timeout, self.cancel,
                                  re=origin.get("id") if origin else None)
        if kind == "wait_agent":
            self.set_phase("waiting @" + str(a.get("from")))
            return self.iface.wait(str(a.get("from") or ""), a.get("wait") or self.args.ask_timeout, self.cancel)
        if kind == "post":
            to = a.get("to") if isinstance(a.get("to"), list) else None
            ok = self.iface.say(str(a.get("text") or ""), to=to)
            return "posted" if ok else "System: not sent (not connected or empty)."
        return (f"Error: unknown action_type '{kind}'. Use one of: {', '.join(ACTION_NAMES)} — "
                "e.g. {\"action_type\": \"check\", \"port\": 8081}")

    def launch(self, a, origin) -> str:
        """Start a service on a host (detached), from the given command or the host's recipe, and wait until it answers."""
        host = self.host_arg(a)
        rport = as_port(a.get("remote_port")) or as_port(a.get("port"))
        e = self.table.get(as_port(a.get("port"))) if a.get("port") else None
        if e and not a.get("remote_port") and e.host == host:
            rport = e.rport  # "port" named the table row: the service listens on its remote port
        if not host or not rport:
            return "Error: launch needs \"host\" and \"port\" (the port the service listens on there)"
        d = self.mem.host(host)
        if not d.get("os"):
            d = self.mem.discovered(host, core.discover(host, self.args.ssh))
            if not d.get("os"):
                return f"Error: {host} not reachable: {(d.get('last_error') or {}).get('err')}"
        service = str(a.get("service") or (e.service if e else "") or "")
        cmd = str(a.get("command") or "").strip()
        recipe = a.get("recipe") if isinstance(a.get("recipe"), dict) and a["recipe"].get("launch") else None
        if not cmd and not recipe:
            recipes = d.get("recipes") or []
            recipe = (next((r for r in recipes if int(r.get("port") or 0) == rport and (not service or r.get("service") == service)), None)
                      or next((r for r in recipes if int(r.get("port") or 0) == rport), None)
                      or next((r for r in recipes if service and r.get("service") == service), None))
            if not recipe:  # what NOTES (or the service name) recalls on this host
                found = self.mem.find_recipes((e.notes if e else "") or service, rport, host)
                recipe = next((f["recipe"] for f in found if f["recipe"].get("launch")), None)
            if not recipe:
                known = ", ".join(f"{r['service']}@{r['port']}" for r in recipes) or "none"
                elsewhere = [f"{h}: {r['service']}@{r['port']}" for h in self.mem.known_hosts() if h != host
                             for r in self.mem.host(h).get("recipes") or []
                             if service and (r.get("service") == service or service.split("/")[-1] in str(r.get("service")))]
                return (f"Error: no command given and no recipe for port {rport} on {host} (its recipes: {known}). "
                        + (f"Other hosts know it: {'; '.join(elsewhere[:4])}. " if elsewhere else "")
                        + "Give \"command\" (the full command line, bound to 127.0.0.1), or discover a host where it runs.")
        if not cmd and recipe:
            cmd = recipe["launch"]
            service = service or recipe.get("service", "")
            if int(recipe.get("port") or 0) != rport:  # the recipe's own port → this one
                cmd = re.sub(r"(--port[ =]|-p[ =])\d+", rf"\g<1>{rport}", cmd)
                cmd = re.sub(rf"(http\.server\s+){int(recipe.get('port') or 0)}\b", rf"\g<1>{rport}", cmd)
        if not cmd:
            return "Error: an empty command line"
        health = core.health_path(a.get("health") or (recipe or {}).get("health") or ("/health" if service.startswith("llm/") else "/"))
        windows = bool(d.get("windows"))
        h0 = core.remote_health(host, rport, health, self.args.ssh)
        if h0.get("code") not in (None, "000"):
            return f"already running: http://127.0.0.1:{rport}{health} on {host} answers HTTP {h0['code']} (nothing launched)"
        cmd, bind_note = core.loopback_guard(cmd)
        if not self.approve(f"launch on {host} (port {rport}): {clip(cmd, 240)}"):
            return "System: denied by the user."
        self.set_phase(f"launching on {host}")
        r = core.launch(host, cmd, rport, windows, self.args.ssh)
        self.mem.event("launch", host=host, port=None, remote=f"127.0.0.1:{rport}", service=service, ok=r["ok"],
                       detail=(f"pid {r.get('pid')}: " if r["ok"] else f"{r.get('err')}: ") + clip(cmd, 200))
        if not r["ok"]:
            return f"Error: launch failed on {host}: {r.get('err')}"
        try:
            wait = max(5, min(int(a.get("wait") or (240 if service.startswith("llm/") else 60)), 900))
        except (TypeError, ValueError):
            wait = 60
        end, code, alive = time.time() + wait, "000", True
        while time.time() < end:
            if self.cancel.is_set():
                raise Cancelled()
            time.sleep(3)
            h = core.remote_health(host, rport, health, self.args.ssh, pid=r["pid"], windows=windows)
            code, alive = h.get("code", "000"), h.get("alive", True)
            self.set_phase(f"launching on {host}: HTTP {code}")
            if code not in ("000", "503") or not alive:
                break
        ok = code not in ("000", "503") and not code.startswith("5")
        if ok:
            self.mem.add_recipe(host, service or f"port{rport}", rport, cmd, health, "launch")
            self.mem.event("launch", host=host, remote=f"127.0.0.1:{rport}", service=service, ok=True,
                           detail=f"answers HTTP {code} after launch")
            fwd = [x for x in self.table.entries() if x.host == host and x.rport == rport]
            up = [x.lport for x in fwd if core.probe(x.lport, x.kind, self.args.probe_timeout)["state"] in ("ok", "loading")]
            nxt = (f"localhost:{', '.join(map(str, up))} already reaches it through its tunnel (up): nothing more to open." if up else
                   f"Now open the row that forwards to it ({', '.join(str(x.lport) for x in fwd)})." if fwd else
                   "No row of the table forwards to it yet: set_entry one, then open it.")
            self.wake.set()
            return (f"launched on {host} (pid {r['pid']}, log {r['log']}" + (f", {bind_note}" if bind_note else "")
                    + f"): http://127.0.0.1:{rport}{health} answers HTTP {code}. {nxt}")
        tail = core.remote_log(host, rport, 15, self.args.ssh)
        if not alive and code in ("000", "503"):
            self.mem.event("launch", host=host, remote=f"127.0.0.1:{rport}", service=service, ok=False,
                           detail=f"exited at once: {clip(tail, 200)}")
            return f"Error: the service exited right after the launch (pid {r['pid']} is gone). Log tail:\n{tail[-1500:]}"
        return (f"Error: launched (pid {r['pid']}) but http://127.0.0.1:{rport}{health} on {host} still says {code} after "
                f"{wait}s ({'loading' if code == '503' else 'not answering'}). Log tail:\n{tail[-1500:]}")


def indent(s: str, pad: str = "  ") -> str:
    return "\n".join(pad + l for l in str(s).split("\n"))


HELP_TEXT = """ptrm — port-remote: services on remote hosts at ports of this machine
  <text>   a request: "I need gemma 12B on 8081", "why is 8086 down?", "what runs on serpenti?", "move 8087 to hostC",
           "show the GPU of hostB", "which host served gemma-4-26B last week?"
  ===r refresh the table now   ===H healing on/off   ===s <words> search memory   ==== reset the context
  ===i open incidents (their diagnosis and checklist)   ===i x <id> close one   ===d <what you did> tell ptrm, recheck now
  ===b [port] bring up a row (all rows without a port)   ===t list teach terminals · ===t <port> open one · ===t x <port> close
  ===off <port> disable a row (ptrm leaves it alone; ~~port~~ in the table) · ===on <port> enable it   ===T LLM thinking on/off
  ===off <host> disable a host (ptrm stops using it) · ===on <host> enable it · ===off alone lists what is disabled
  ===a auto-approve (remote/local commands, launches, editor)   ===x cancel the running task
  ===n network   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
  DASH: <CR> check (an INCIDENTS line: its details) · o open · x close · R reopen · B bring up · T teach · D diagnose · X disable /
        enable the row (a HOSTS line: the host) · Xh disable / enable the host · s stats (the row's host) · S stats (this machine) · t a terminal on the host (type commands there) · d discover
        (+ inventory) · h history · e edit the table · r refresh
        STATS: x close the pane · R restart it
  <Tab> next pane · <S-Tab> previous · i input · <C-c> cancel · q back to your tab"""


def autopilot_allows() -> bool:
    path = os.path.expanduser("~/echo-home/autopilot.yaml")
    try:
        import yaml
        data = yaml.safe_load(open(path)) or {}
        v = (data.get("ptrm") or {}).get("allow")
        return v is True or str(v).lower() in ("yes", "true", "on")
    except Exception:
        return False


def reader(agent: Agent):
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        t = msg.get("t")
        if t == "input":
            agent.on_input(msg.get("text", ""), msg.get("ctx"))
        elif t == "op":
            agent.on_op(msg)
        elif t == "res":
            agent.on_result(msg)
        elif t == "net":
            op = msg.get("op")
            if op == "join":
                agent.iface.join(msg.get("name"), msg.get("purpose"), msg.get("hub"), msg.get("network"))
            elif op == "leave":
                agent.iface.leave()
            elif op == "say":
                agent.iface.say(msg.get("text", ""), to=msg.get("to"))
        elif t == "cancel":
            agent.do_cancel()
        elif t == "quit":
            break
    agent.running = False
    agent.iface.net.close()
    emit({"t": "exit"})


def main():
    global ARGS
    p = argparse.ArgumentParser(description="ptrm: the port-remote agent (backend of ptrm.lua)")
    p.add_argument("--table", default=os.environ.get("PTRM_TABLE", ""),
                   help="ptrm.md (default: ~/echo-home/.vim/pages/<hostname>.ptrm.md, else ptrm.md)")
    p.add_argument("--memory", default=os.environ.get("PTRM_HOME") or core.DEFAULT_MEMORY)
    p.add_argument("--ssh", default="ssh")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between two looks at the table's ports")
    p.add_argument("--probe-timeout", type=float, default=2.5)
    p.add_argument("--open-wait", type=float, default=12.0, help="seconds a new tunnel has to come up")
    p.add_argument("--no-heal", dest="heal", action="store_false", help="do not reopen tunnels whose ssh died")
    p.add_argument("--heal-tries", type=int, default=6)
    p.add_argument("--no-incidents", dest="incidents", action="store_false",
                   help="do not open incidents (diagnose, fix, ask for remote hands) when a row that was up fails")
    p.add_argument("--incident-grace", type=float, default=12.0, help="seconds a row stays bad before an incident opens")
    p.add_argument("--incident-approve-timeout", type=int, default=300,
                   help="seconds an incident waits for the approval of a relaunch (then: no, and remote hands)")
    p.add_argument("--hands-remind", type=float, default=30.0, help="minutes between reminders of a remote-hands request (3 at most)")
    p.add_argument("--no-bringup", dest="bringup", action="store_false",
                   help="at start, do not bring up the rows of the table (open, launch from what NOTES recalls, teach)")
    p.add_argument("--teach-hours", type=float, default=3.0, help="how long a teach terminal is watched for the service")
    p.add_argument("--inventory-age", type=float, default=24.0,
                   help="hours after which a reachable host's inventory is taken again by itself (0: never)")
    p.add_argument("--no-llm-tunnel", dest="llm_tunnel", action="store_false",
                   help="do not open the LLM's own row at start when the LLM port is a row of the table and closed")
    p.add_argument("--port", type=int, default=8080, help="port of the LLM server")
    p.add_argument("--host", default="localhost")
    p.add_argument("--think", dest="think", action="store_true", default=True,
                   help="let the LLM think; its thinking streams into the LLM pane (the default)")
    p.add_argument("--no-think", dest="think", action="store_false", help="no thinking: faster, shorter turns")
    p.add_argument("--auto", action="store_true", help="auto-approve (also autopilot.yaml ptrm.allow)")
    p.add_argument("--max-turns", type=int, default=14)
    p.add_argument("--max-output", type=int, default=3500)
    p.add_argument("--max-history", type=int, default=30000, help="chars of conversation kept for the LLM")
    p.add_argument("--remote-timeout", type=int, default=60)
    p.add_argument("--cmd-timeout", type=int, default=30)
    p.add_argument("--ask-timeout", type=int, default=180)
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("PTRM_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    g.add_argument("--net-readonly", action="store_true", help="network requests may look but not change anything")
    ARGS = p.parse_args()

    agent = Agent(ARGS)
    rows = agent.table.entries()
    chat(f"ptrm · port-remote · {len(rows)} rows in {agent.table.path}", "Title")
    chat(f"   memory {agent.mem.root}: {len(agent.mem.known_hosts())} hosts · {len(agent.mem.known_ports())} ports known", "Comment")
    if ARGS.llm_tunnel and not agent.llm.ensure() and ARGS.host in ("localhost", "127.0.0.1"):
        e = agent.table.get(ARGS.port)
        if e:
            chat(f"   the LLM port {ARGS.port} is a row of the table and closed: opening it ({e.host} {e.remote})", "Comment")
            chat("   " + agent.open_entry(e, how="open"), "Comment")
    llm_ok = agent.llm.ensure()
    chat(f"LLM {agent.llm.base}: " + (agent.llm.model if llm_ok else "not reachable yet (start it; I retry on each request)"),
         "Comment" if llm_ok else "WarningMsg")
    chat("Tell me which service you need at which port, or ask why one is down. ===h help · <Tab> next pane", "Comment")
    logw("LLM — raw LLM output, streamed as it is generated", "Title")
    iface_log("IFACE — Intelligence Network traffic and decisions (shown while joined)", "Title")
    import ptrm_iface
    if ptrm_iface.INET_ERROR:
        iface_log("⚠️ Intelligence Network driver not loaded; ptrm works locally but cannot join.", "ErrorMsg")
        iface_log("   " + ptrm_iface.INET_ERROR, "ErrorMsg")
    else:
        iface_log(f"   inet driver: {ptrm_iface.INET_DIR}", "Comment")
    if agent.mem.migrated:
        chat(f"   memory moved into the per-client folder {agent.mem.root} ({', '.join(agent.mem.migrated)})", "Comment")
    if agent.mem.peers():
        chat(f"   also reading the memory of {', '.join(os.path.basename(p) for p in agent.mem.peers())}", "Comment")
    if ARGS.bringup:  # the table is what the user wants at those ports: bring every row up, in its order
        rows = [e for e in agent.table.entries() if not e.disabled]  # a disabled row is left alone
        off = [e for e in rows if not e.auto_host and e.host in agent.off_hosts]  # … and a row on a disabled host
        for h in sorted({e.host for e in off}):
            chat(f"🚫 host {h} is disabled: I leave {', '.join(str(e.lport) for e in off if e.host == h)} alone", "Comment")
        rows = [e for e in rows if e not in off]
        if rows:
            chat(f"⏫ bringing up the table: {', '.join(str(e.lport) for e in rows)}", "Title")
        for e in rows:
            agent.enqueue_bringup(e.lport, auto=True)
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.op_worker, daemon=True).start()
    threading.Thread(target=agent.watcher, daemon=True).start()
    if ARGS.net_name:
        agent.iface.join()
    agent.emit_state()
    reader(agent)


if __name__ == "__main__":
    main()
