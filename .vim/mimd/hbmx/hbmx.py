#!/usr/bin/env python
"""
hbmx: the large-file reader and data analyst agent. Headless backend of hbmx.lua (same folder).

hbmx never loads a big file into the LLM context. The engine (hbmx_core.py, hbmx_ops.py) probes a file with the
environment's signature tool (libmagic `file`) and its own detectors, maps it in one streaming pass and keeps the
map, helper functions and findings in the file's memory (~/echo-home/memory/hbmx/<d1>/<d2>/<d3d4>/<sha256>/: meta.yaml,
.function/, .data/). The LLM works only through actions whose results are small, structured texts;
the full results go to the user's VIEW pane, the map to the MAP pane. Pane navigation (<CR> on a zone, region,
landmark or line) runs the engine directly, without the LLM.

Shaped after mksk.py / knbg.py (same protocol style, steering, approvals, Intelligence Network iFace).

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"w","b":"chat"|"iface"|"log","text":..,"hl":<highlight group>|null,"raw":b}   append (raw: continue the last line)
  {"t":"pane","b":"map"|"view","title":..,"text":..}                                    replace the MAP / VIEW pane
  {"t":"state","busy","phase","queue","auto","approval","file":{name,path,sha,fmt}|null,"net":{..},"model":..}
  {"t":"show"}  {"t":"notify","text":..,"level":"info"|"warn"}  {"t":"exit"}
nvim -> backend
  {"t":"input","text":..,"ctx":{pane,line,text}|null,"sel":{file,start,end,text}|null}
  {"t":"open","path":..}                                    open/probe a file now (no LLM)
  {"t":"nav","op":"line"|"byte"|"zone"|"region"|"landmark"|"time"|"next"|"prev"|"function"|"mark"|"map","arg":..,
   "count":N,"label":..}                                    pane navigation (no LLM)
  {"t":"net","op":"join"|"leave"|"say",...}  {"t":"cancel"}  {"t":"interrupt"}  {"t":"quit"}
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
for _stream in (sys.stdin, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import hbmx_core as C  # noqa: E402
import hbmx_know as K  # noqa: E402
import hbmx_ops as O  # noqa: E402

ARGS: Optional[argparse.Namespace] = None

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
    emit({"t": "w", "b": "chat", "text": str(text), "hl": hl})


def ilog(text: str = "", hl: Optional[str] = None, quiet: bool = False):
    emit({"t": "w", "b": "iface", "text": str(text), "hl": hl, "quiet": quiet})


def logw(text: str = "", hl: Optional[str] = None, quiet: bool = False):
    emit({"t": "w", "b": "log", "text": str(text), "hl": hl, "quiet": quiet})


def pane(b: str, title: str, text: str):
    lines = text.split("\n")
    if len(lines) > ARGS.pane_lines:
        text = "\n".join(lines[:ARGS.pane_lines]) + f"\n… [{len(lines) - ARGS.pane_lines} more lines not shown]"
    emit({"t": "pane", "b": b, "title": title, "text": text})


clip = C.clip


def parse_llm_json(raw: str) -> Optional[dict]:
    """First balanced JSON object in the text (tolerates fences, prose, <think> blocks)."""
    if not raw:
        return None
    raw = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw, flags=re.S)
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
                        obj = json.loads(raw[start:i + 1], strict=False)
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
    return None


# ---------------------------------------------------------------- LLM
class LLM:
    def __init__(self, host, port):
        from openai import OpenAI
        self.base = f"http://{host}:{port}/v1"
        self.client = OpenAI(base_url=self.base, api_key="localm", timeout=600)
        self.model = None

    def ensure(self) -> bool:
        if self.model:
            return True
        try:
            models = self.client.models.list()
            self.model = models.data[0].id if models.data else None
        except Exception:
            self.model = None
        return bool(self.model)

    def stream(self, messages, interrupt: threading.Event) -> Tuple[str, bool]:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base} (start it, then ask again)")
        resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.0, stream=True)
        out, buf, last, interrupted, in_think = "", "", time.time(), False, False

        def flush():
            nonlocal buf, last
            if buf:
                emit({"t": "w", "b": "log", "text": buf, "raw": True})
                buf, last = "", time.time()
        try:
            for chunk in resp:
                if interrupt.is_set():
                    interrupted = True
                    break
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                think = getattr(d, "reasoning_content", None)
                if think:
                    if not in_think:
                        flush()
                        buf, in_think = "💭 ", True
                    buf += think
                if d.content:
                    if in_think:
                        buf += "\n"
                        in_think = False
                    out += d.content
                    buf += d.content
                if "\n" in buf or len(buf) > 160 or time.time() - last > 0.1:
                    flush()
        finally:
            flush()
            emit({"t": "w", "b": "log", "text": "", "hl": None})
            if interrupted:
                try:
                    resp.close()
                except Exception:
                    pass
        return out, interrupted

    def json_call(self, system: str, user: str) -> Optional[dict]:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        return parse_llm_json(r.choices[0].message.content or "")


# ---------------------------------------------------------------- prompts
SYSTEM_PROMPT = """You are hbmx, a data analyst for LARGE files, working with the user inside Neovim. You never read a big file whole: every look at the data is an action that returns a small, structured result (its header says what was read and whether it was exact or a SAMPLE). Each file's memory (map, helper functions, findings) persists under ~/echo-home/memory/hbmx and is reused on every later read.

HOW YOU WORK
1. Each turn output ONE JSON object (one action), then read its [RESULT]. Finish every request with declare_result.
2. Start from the map. [CURRENT FILE] shows identity, structure, regions (index tree r1, r1.2 …), zones (uniform grid z1…zN with error density), landmarks (L1 …), functions and howto. Zoom (map zone=…/region=…), count and aggregate (stats, search with count), then look at SMALL windows (view count ≤ 40) where the numbers point. Never page through a file to read it.
3. Playbooks:
   - summary/overview: stats by format (levels | columns | keys | templates) → view 2-3 small samples from different zones → declare_result with a structured summary.
   - log analysis: stats levels → stats timeline → when a time or zone stands out (spike, hotspot landmark), stats compare lines=<that zone> tells WHAT changed there (a level, a pattern) → search the top error patterns → view context around landmarks.
   - security audit: stats security → search each indicator for context → stats top (field=ip, or regex) → note findings.
   - pattern discovery: stats templates / top / keys / columns; stats compare lines=zN shows what is over-represented in a zone vs the whole file.
   - specific records or times: search / filter / seek time / record.
4. Numbers come from stats/search/filter counts, never from counting lines you viewed. Write "≈" when a result says SAMPLE; pass "exact":true when precision matters on a big file.
5. Make helpers: when a lookup will repeat or needs logic the actions lack, write_function (bash or python) that reads $HBMX_FILE, takes args, prints bounded structured output (TSV or JSON lines, at most 100 lines) and never modifies the file; then run_function. Functions stay with the file for later reads.
6. Knowledge: [KNOWLEDGE] is what earlier analyses of this same content established (same sha256, so it still holds). When it answers the request, answer from it (say it is from memory, cite its K/Q ids) instead of analysing again; otherwise build on it; recall searches all of it. Every answered question is kept and its key findings are extracted for you; note kind=finding keeps an important conclusion word for word, kind=landmark marks a place worth returning to, kind=howto says how to handle this file next time.
7. Full results go to the user's VIEW pane; you get them clipped. The "reply" of declare_result IS the answer the user reads: put the facts in it (numbers, times, patterns, line references like line 1,234) in short lines. Never reply only "done" or "see the finding".
8. "User Steering Update" messages come from the user while you work and override earlier plans. [SELECTION] is text the user selected; [USER POSITION] is where the user's cursor is.
9. No file open and the request names one: open it first. Output ONLY one JSON object, no markdown around it.

ACTIONS ("action_type" plus fields at the top level; every action may add "reason": one short sentence)
Files:
  {"action_type":"open","path":"/var/log/app.log"}          probe + map (instant when known); it becomes the current file
  {"action_type":"files"}                                   files in memory, newest first ("@<sha prefix>" opens one too)
Looking:
  {"action_type":"map","zone":"z7"}                         the full map; "zone" or "region" zooms in (both optional)
  {"action_type":"view","line":1200,"count":30}             numbered lines; or "region":"r1.2"|"z7"|"L3"; or "time":"2026-01-02T10:00..2026-01-02T10:05"; or "byte":4096,"length":256 (hex)
  {"action_type":"record","line":1200}                      one record parsed into fields
  {"action_type":"seek","time":"2026-01-02T10:00"}          or "line"/"byte": the position, with context
  {"action_type":"search","pattern":"timeout","regex":true,"ignore_case":false,"max":20,"lines":"z7","count":true}
  {"action_type":"filter","where":["status>=500","path~^/api"],"fields":["ts","path"],"max":20}   ops: = != > >= < <= ~ !~ ^= *=
  {"action_type":"stats","kind":"levels|timeline|columns|keys|templates|security|top|compare","lines":"z7","where":["level=ERROR"],"exact":false}
        top needs "field":"status" (or "regex":"user=(\\\\w+)"); compare needs "lines" (target) and optional "baseline"; "where" filters records first
Making:
  {"action_type":"extract","pattern":"ERROR","name":"errors"}    or "lines":"A:B" | "time":"T1..T2" | "where":[..]; writes a file in memory and returns its path (open it to analyse it)
  {"action_type":"inflate"}                                  compressed file → random-access copy, mapped
  {"action_type":"functions"}
  {"action_type":"write_function","name":"slow_requests","lang":"python","description":"..","usage":"slow_requests [ms]","code":"..."}
  {"action_type":"run_function","name":"slow_requests","args":["500"]}
  {"action_type":"shell","cmd":"rg -c 'x' \\"$HBMX_FILE\\""}     a read-only command, the user approves it; output clipped
Memory:
  {"action_type":"recall","query":"error spike"}            what earlier analyses of this content found about it
  {"action_type":"note","kind":"finding","title":"..","detail":"..","refs":["line 3412"]}
  {"action_type":"note","kind":"landmark","label":"deploy restart","line":81234}
  {"action_type":"note","kind":"howto","text":".."}
Finish:
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
"""

NET_PROMPT = """
NETWORK — you are on the Intelligence Network <NETWORK> as @<NAME>; [NETWORK] lists the members.
- Requests marked [Network request …] come from other agents or humans asking you to analyse files on this machine. The "reply" of declare_result is sent back to them: numbers, line refs, paths of extracts. The user is not part of that exchange.
- shell and running functions you wrote need the user's approval, which may be slow: prefer the built-in actions for network requests.
Network actions:
  {"action_type":"who"}
  {"action_type":"post","text":"..","to":["name"]}            (to optional = broadcast)
  {"action_type":"ask_agent","to":"memphia","text":"..","wait":120}
  {"action_type":"wait_agent","from":"memphia","wait":120}
"""

HELP_TEXT = """hbmx commands (chat)
  <text>        idle: new request │ busy: steering for the next turn
  !<text>       steer and interrupt the generation now     +<text>  queue a request after the current one
  ===o <path>   open (probe + map) a file without the LLM   ===f  files in memory
  ===s          sessions: one conversation per file (hbmxo / ===o on a file brings its conversation back)
  ===k          the file's knowledge in VIEW               ===k <text>  keep <text> as a finding of yours
  fresh: <q>    analyse again instead of answering from memory (a repeated question is answered from memory)
  ===m          redraw the MAP pane                        ===a  auto-approve shell/functions on/off
  ===x          cancel the running request                 ====  clear queues
  ===r          forget the conversation                    ===c  clear the chat   ===h  this help
  ===n          network status   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
Answers: approvals y │ n │ a (always, this session)
Panes: MAP (left) / VIEW (right): <CR> on z7, r1.2, L3, a line number or a function → shown in VIEW (no LLM)
       VIEW: ] / [ next / previous page · gl go to line · gt go to time · gb go to byte · m mark a landmark here
Bottom tabs (click one, or <Tab>): HBMX · <file> = this chat │ the raw LLM output, side by side; IFACE = the network
  (full width); • / ●n = news you have not seen
Keys: C-x interrupt │ C-c cancel │ i back to the chat │ q back to the previous tab"""

ACTIONS = ["open", "files", "map", "view", "record", "seek", "search", "filter", "stats", "extract", "inflate",
           "functions", "write_function", "run_function", "shell", "note", "recall", "who", "post", "ask_agent",
           "wait_agent", "declare_result"]
DATA_ACTIONS = {"view", "record", "seek", "search", "filter", "stats", "extract", "run_function", "shell"}
NET_ACTIONS = {"who", "post", "ask_agent", "wait_agent"}
ALIASES = {"grep": "search", "find": "search", "read": "view", "show": "view", "head": "view", "lines": "view",
           "probe": "open", "load": "open", "analyze": "stats", "analyse": "stats", "count": "search",
           "finish": "declare_result", "done": "declare_result", "answer": "declare_result", "result": "declare_result",
           "run": "run_function", "function": "run_function", "remember": "note", "list_files": "files",
           "zoom": "map", "aggregate": "stats", "summary": "stats", "memory": "recall", "knowledge": "recall"}


def normalize_action(act: dict) -> dict:
    """Near-misses small models produce: "action":"x", "tool:view", aliases, {"view": {...}}."""
    kind = str(act.get("action_type") or act.get("action") or act.get("tool") or "").strip().lower()
    for sep in (":", ".", "/", " "):
        kind = kind.rsplit(sep, 1)[-1].strip() if kind not in ACTIONS else kind
    kind = ALIASES.get(kind, kind)
    if kind in ACTIONS:
        out = dict(act, action_type=kind)
        if kind == "stats" and not out.get("kind") and str(act.get("action_type", "")).lower() == "summary":
            out["kind"] = "overview"
        return out
    for name in ACTIONS:
        if isinstance(act.get(name), dict):
            out = {k: v for k, v in act.items() if k not in (name, "action_type", "action")}
            out.update(act[name])
            out["action_type"] = name
            return out
    return act


class Cancelled(Exception):
    pass


def autopilot_allows() -> bool:
    try:
        import yaml
        with open(os.path.expanduser("~/echo-home/autopilot.yaml")) as f:
            v = ((yaml.safe_load(f) or {}).get("hbmx") or {}).get("allow")
        return v is True or str(v).lower() in ("yes", "true", "on")
    except Exception:
        return False


def _int(v, default=None):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _bool(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- the agent
class Agent:
    def __init__(self, args):
        self.args = args
        self.llm = LLM(args.host, args.port)
        self.lock = threading.RLock()
        self.tasks: deque = deque()
        self.steering: deque = deque()
        self.wake = threading.Event()
        self.cancel_ev = threading.Event()
        self.interrupt_ev = threading.Event()
        self.running = True
        self.current: Optional[dict] = None
        self.phase = "idle"
        self.auto = args.auto or autopilot_allows()
        self.approval: Optional[dict] = None
        self.sessions: Dict[str, dict] = {}       # one conversation per file (key: sha256; "_" before any file)
        self.skey = "_"
        self._session("_")
        self.req_steps: List[dict] = []           # the running request's actions + results (its evidence trail)
        self.req_findings: List[dict] = []
        self.req_start = 0
        self.req_body = ""
        self.e: Optional[C.Entry] = None          # the current file
        self.last_view: Optional[dict] = None     # what VIEW shows, for ] / [
        self.navq: "queue.Queue" = queue.Queue()
        self._phase_t = 0.0
        from hbmx_iface import IFace
        self.iface = IFace(self, args, self.llm.json_call, ilog)

    # ----- per-file sessions
    def _session(self, key: str) -> dict:
        s = self.sessions.get(key)
        if s is None:
            s = self.sessions[key] = {"history": [], "trim_told": False, "name": None}
        return s

    @property
    def history(self) -> List[dict]:
        return self.sessions[self.skey]["history"]

    @property
    def trim_told(self) -> bool:
        return self.sessions[self.skey]["trim_told"]

    @trim_told.setter
    def trim_told(self, v: bool):
        self.sessions[self.skey]["trim_told"] = v

    def switch_session(self, e: C.Entry):
        """The conversation follows the current file: each file has its own, kept while hbmx runs."""
        key, prev = e.sha, self.skey
        new = key not in self.sessions
        s = self._session(key)
        s.update(name=e.name, path=e.path)
        if key == prev:
            return
        prev_name = self.sessions[prev].get("name") or "no file"
        self.skey = key
        emit({"t": "session", "key": key[:16], "name": e.name, "fmt": e.fmt, "new": new})
        logw(f"\n── session {e.name} ──", "Title", quiet=True)
        if new:
            chat(f"◆ {e.name} · this file's own conversation (===s lists the sessions)", "Title")
        else:
            chat(f"↩ back to {e.name}: its conversation is restored ({len(s['history'])} messages in my context)", "Comment")
        if self.current and self.current.get("kind") == "task" and self.req_body:  # a request opened this file
            self.history.append({"role": "user", "k": "req",
                                 "content": f"System: continued here from a request made on {prev_name}.\n{self.req_body}"})
            self.req_start = len(self.history)

    # ----- state
    @property
    def busy(self) -> bool:
        return self.current is not None

    def emit_state(self):
        e = self.e
        emit({"t": "state", "busy": self.busy, "phase": self.phase, "queue": len(self.tasks), "auto": self.auto,
              "approval": self.approval["text"] if self.approval else None, "model": self.llm.model,
              "file": {"name": e.name, "path": e.path, "sha": e.sha[:12], "fmt": e.fmt} if e else None,
              "net": self.iface.summary()})

    def set_phase(self, p: str):
        self.phase = p
        self.emit_state()

    def progress(self, phase: str, frac: float):
        if time.time() - self._phase_t > 0.3:
            self._phase_t = time.time()
            self.set_phase(phase)

    def ctl(self) -> C.Ctl:
        return C.Ctl(self.progress, self.cancel_ev)

    # ----- for the iFace
    def submit(self, text, origin=None):
        self.submit_task(text, origin=origin)

    def has_net_work(self) -> bool:
        with self.lock:
            return bool(self.current and self.current.get("origin")) or any(t.get("origin") for t in self.tasks)

    def describe_busy(self) -> str:
        cur = self.current
        return (f"busy with: {clip(cur.get('text'), 160)}" if cur else "idle") + f"; {len(self.tasks)} queued"

    def digest(self) -> str:
        cur = f"current file: {self.e.path} ({self.e.fmt}, {C.human(self.e.size)})" if self.e else "no file open"
        rows = sorted(C.catalog_load()["files"].items(), key=lambda kv: kv[1].get("last_read", ""), reverse=True)[:8]
        return cur + "; files in memory: " + ("; ".join(f"{os.path.basename(p)} ({r.get('format')})" for p, r in rows) or "none")

    # ----- input
    def submit_task(self, text, sel=None, ctx=None, origin=None, kind="task", front=False, **extra):
        task = dict(extra, kind=kind, text=text, sel=sel, ctx=ctx, origin=origin)
        with self.lock:
            (self.tasks.appendleft if front else self.tasks.append)(task)
        self.wake.set()
        self.emit_state()

    def on_input(self, text: str, ctx=None, sel=None):
        clean = (text or "").strip()
        if clean == "===x":
            return self.cancel()
        if self.approval:
            self.approval["answer"] = clean.lower()
            self.approval["event"].set()
            return
        if not clean:
            return
        if clean.startswith("==="):
            return self.command(clean)
        body = clean[1:].strip() if clean[:1] in "+!" else clean
        chat("")
        chat("▶ " + body, "Title")
        if not self.busy:
            self.submit_task(body, sel, ctx)
        elif clean.startswith("+") or not (self.current or {}).get("kind") == "task" or (self.current or {}).get("origin"):
            self.submit_task(body, sel, ctx)
            chat("  ⏳ queued after the current request", "Comment")
        else:
            if sel:
                body += f"\n[SELECTION] {sel.get('file')} lines {sel.get('start')}-{sel.get('end')}:\n{clip(sel.get('text'), 3000)}"
            with self.lock:
                self.steering.append(body)
            if clean.startswith("!"):
                self.interrupt_ev.set()
            else:
                chat("  ↪ steering queued for the next turn", "Comment")

    def command(self, cmd: str):
        if cmd == "===h":
            chat(HELP_TEXT, "Comment")
        elif cmd == "====":
            with self.lock:
                s, t = len(self.steering), len(self.tasks)
                self.steering.clear()
                self.tasks.clear()
            chat(f"cleared {s} steering, {t} queued request(s)", "Comment")
            self.emit_state()
        elif cmd == "===r":
            self.history.clear()
            self.trim_told = False
            chat("this file's conversation is cleared from my context (its map and knowledge are kept)", "Comment")
        elif cmd == "===s":
            rows = [f"  {'▶' if k == self.skey else ' '} {s.get('name') or '(before any file)'} · {len(s['history'])} messages"
                    for k, s in self.sessions.items()]
            chat("sessions — one conversation per file (hbmxo / ===o on a file brings its conversation back):\n"
                 + "\n".join(rows), "Comment")
        elif cmd.startswith("===k"):
            text = cmd[4:].strip()
            if not self.e:
                chat("no file is open", "Comment")
            elif text:
                fid, added = K.add_finding(self.e, text, "", by="user")
                chat(f"🧠 {fid} {'kept' if added else 'was already known: confirmed again'}: {text}", "Comment")
                self.note_hist(f"System: the user recorded a finding about {self.e.name}: {text}")
                self.show_map()
            else:
                self.show_view(f"VIEW · knowledge of {self.e.name}", K.render_all(self.e))
        elif cmd == "===c":
            emit({"t": "clear", "b": "chat"})
        elif cmd == "===a":
            self.auto = not self.auto
            chat(f"auto-approve shell / your functions: {'ON' if self.auto else 'off'}", "WarningMsg")
            self.emit_state()
        elif cmd == "===f":
            chat(C.files(30), "Comment")
        elif cmd == "===m":
            self.show_map()
        elif cmd.startswith("===o"):
            path = cmd[4:].strip()
            if path:
                self.open_request(path)
            else:
                chat("===o <path>", "Comment")
        elif cmd.startswith("===n"):
            self.net_command(cmd[4:])
        else:
            chat(f"unknown command {cmd} — ===h for help", "Comment")

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

    def open_request(self, path: str):
        """Open from the UI (:HbmxOpen, ===o): a task of its own, no LLM."""
        self.submit_task(f"open {path}", kind="open", path=path)

    def cancel(self):
        if self.busy:
            self.cancel_ev.set()
            self.interrupt_ev.set()
            if self.approval:
                self.approval["answer"] = "n"
                self.approval["event"].set()
            chat("✖ cancelling…", "WarningMsg")
        else:
            chat("nothing is running", "Comment")

    def interrupt(self):
        if self.busy:
            self.interrupt_ev.set()
            chat("⏹ interrupt requested", "WarningMsg")

    def shutdown(self):
        self.running = False
        self.cancel_ev.set()
        self.interrupt_ev.set()
        self.wake.set()
        self.navq.put(None)

    # ----- approvals
    def approve(self, what: str) -> bool:
        if self.auto:
            chat(f"  ✓ auto-approved: {clip(what, 200)}", "Comment")
            return True
        ev = threading.Event()
        self.approval = {"text": what, "event": ev, "answer": ""}
        chat(f"  ❓ allow {what} ?  [y]es / [n]o / [a]lways", "WarningMsg")
        emit({"t": "show"})
        self.set_phase("waiting for you")
        while not ev.wait(0.2):
            if self.cancel_ev.is_set() or not self.running:
                break
        ans = self.approval["answer"] if self.approval else ""
        self.approval = None
        if self.cancel_ev.is_set():
            raise Cancelled()
        if ans.startswith("a"):
            self.auto = True
        ok = ans[:1] in ("y", "a")
        chat("    approved" if ok else "    denied", "Comment")
        self.set_phase("working")
        return ok

    # ----- panes
    def show_map(self):
        if not self.e:
            pane("map", "MAP", "no file open\n\n:HbmxOpen <path>  or ask: \"open /var/log/syslog and summarise it\"\n\n" + C.files(20))
            return
        try:
            pane("map", f"MAP · {self.e.name} · {self.e.fmt}", C.render_map(self.e))
        except Exception as ex:
            pane("map", "MAP", f"cannot render the map: {type(ex).__name__}: {ex}")

    def show_view(self, title: str, text: str):
        pane("view", title, text)

    # ----- the worker (requests, opens) and the navigator (pane clicks)
    def worker(self):
        self.show_map()
        while self.running:
            self.wake.wait(0.2)
            with self.lock:
                task = self.tasks.popleft() if self.tasks else None
                if not self.tasks:
                    self.wake.clear()
            if not task or not self.running:
                continue
            self.cancel_ev.clear()
            self.interrupt_ev.clear()
            self.current = task
            self.set_phase("working")
            status, reply = "FAILED", ""
            try:
                if task["kind"] == "open":
                    self.do_open(task["path"], announce=True)
                    status = None
                else:
                    status, reply = self.run_request(task)
            except (Cancelled, C.Cancelled):
                status, reply = "CANCELLED", "Cancelled."
                chat("🛑 cancelled", "WarningMsg")
                self.note_hist("System: the user cancelled that request.")
            except Exception as ex:
                status, reply = "FAILED", f"{type(ex).__name__}: {ex}"
                chat(f"❌ {reply}", "ErrorMsg")
                logw(f"[AGENT ERROR] {reply}", "ErrorMsg")
            finally:
                with self.lock:
                    left = list(self.steering)
                    self.steering.clear()
                if left and status != "CANCELLED":
                    chat("  ↪ your steering came after the request finished: it is the next request", "Comment")
                    self.submit_task("\n".join(left), front=True)
                origin = task.get("origin")
                if origin and status:
                    self.iface.answer(origin, status, reply)
                elif status and task["kind"] == "task":
                    emit({"t": "notify", "text": f"hbmx {status}: {reply[:120]}", "level": "info" if status == "SUCCESS" else "warn"})
                self.current = None
                self.set_phase("idle")
                if self.iface.connected:
                    self.iface.set_state(self.iface.idle_state())

    def navigator(self):
        while self.running:
            msg = self.navq.get()
            if msg is None:
                break
            try:
                self.nav(msg)
            except (C.HbmxError, ValueError) as ex:
                self.show_view("VIEW · error", f"# {ex}")
            except Exception as ex:
                self.show_view("VIEW · error", f"# {type(ex).__name__}: {ex}")

    def nav(self, m: dict):
        e = self.e
        op, arg = m.get("op"), m.get("arg")
        count = max(5, min(_int(m.get("count"), 40), O.MAX_VIEW))
        if op == "map":
            return self.show_map()
        if not e:
            return self.show_view("VIEW", "# no file open (:HbmxOpen <path>)")
        ctl = C.Ctl(None, None)
        if op == "line":
            n = max(1, _int(arg, 1))
            start = max(1, n - 3) if m.get("center") else n
            self.view_lines(start, count)
        elif op == "byte":
            b = _int(arg, 0)
            if e.random_access and not m.get("hex"):
                return self.view_lines(max(1, e.byte_line(b) - 3), count)
            self.last_view = {"kind": "byte", "pos": b, "count": count * 16}
            self.show_view(f"VIEW · bytes {C.num(b)}", O.hexview(e, b, count * 16))
        elif op in ("zone", "region"):
            node = e.find_node(arg)
            if not node:
                raise C.HbmxError(f"unknown {op} {arg}")
            if str(arg).startswith("z") and "." not in str(arg):
                text = C.render_map(e, zone=arg)
                if node.get("lines"):
                    text += "\n\n" + O.view(e, line=node["lines"][0], count=min(count, 20))
                self.show_view(f"VIEW · zone {arg}", text)
            elif node.get("children"):
                self.show_view(f"VIEW · region {arg}", C.render_map(e, region=arg))
            elif node.get("lines"):
                self.view_lines(node["lines"][0], count, extra=f" · {arg}")
            elif node.get("bytes"):
                self.last_view = {"kind": "byte", "pos": node["bytes"][0], "count": count * 16}
                self.show_view(f"VIEW · {arg}", O.hexview(e, node["bytes"][0], count * 16))
            else:
                self.show_view(f"VIEW · {arg}", f"# {arg} {node.get('name')}\n  {node.get('what') or ''}")
        elif op == "landmark":
            lm = e.landmark(arg)
            if not lm:
                raise C.HbmxError(f"unknown landmark {arg}")
            if lm.get("line") and e.text:
                self.view_lines(max(1, lm["line"] - 3), count, extra=f" · {arg} {clip(lm['label'], 40)}")
            else:
                b = max(0, int(lm.get("byte") or 0) - 32)
                self.last_view = {"kind": "byte", "pos": b, "count": count * 16}
                self.show_view(f"VIEW · {arg}", O.hexview(e, b, count * 16))
        elif op == "time":
            t = C.parse_ts_text(arg, e.year())
            if t is None:
                raise C.HbmxError(f"cannot read the time {arg!r}")
            line = O.seek_time_line(e, t, ctl)
            if line is None:
                raise C.HbmxError(f"nothing at or after {arg}")
            self.view_lines(line, count, extra=f" · first line at/after {C.iso(t)}")
        elif op in ("next", "prev"):
            lv = self.last_view
            if not lv:
                return
            sign = 1 if op == "next" else -1
            if lv["kind"] == "lines":
                self.view_lines(max(1, lv["pos"] + sign * lv["count"]), lv["count"])
            else:
                b = max(0, lv["pos"] + sign * lv["count"])
                self.last_view = dict(lv, pos=b)
                self.show_view(f"VIEW · bytes {C.num(b)}", O.hexview(e, b, lv["count"]))
        elif op == "function":
            self.show_view(f"VIEW · function {arg}", O.function_source(e, str(arg)))
        elif op == "knowledge":
            text = K.render_record(e, str(arg)) if arg else K.render_all(e)
            self.show_view(f"VIEW · knowledge {arg or ''}", text)
        elif op == "mark":
            line = _int(arg)
            label = str(m.get("label") or "").strip()
            if not (line and label):
                return
            out = O.note(e, "landmark", by="user", label=label, line=line)
            chat(f"📍 {out.lstrip('# ')}", "Comment")
            self.note_hist(f"System: the user marked a landmark on {e.name}: {label} (line {line}).")
            self.show_map()

    def view_lines(self, start: int, count: int, extra: str = ""):
        e = self.e
        text = O.view(e, line=start, count=count)
        self.last_view = {"kind": "lines", "pos": start, "count": count}
        self.show_view(f"VIEW · {e.name} · lines {C.num(start)}–{C.num(start + count - 1)}{extra}", text)

    # ----- opening a file
    def do_open(self, path: str, announce: bool = False) -> str:
        path = os.path.expanduser(str(path or "").strip().strip("'\""))
        if not path:
            raise C.HbmxError("open needs a path")
        if not path.startswith("@") and not os.path.isabs(path):
            path = os.path.join(self.args.cwd, path)
        if path.startswith("@"):
            e, fresh = C.resolve(path), False
        else:
            if not os.path.isfile(path):
                raise C.HbmxError(f"not a file: {path}")
            size = os.path.getsize(path)
            self.set_phase(f"probing {C.human(size)}")
            e, fresh = C.probe(path, ctl=self.ctl())
        self.e = e
        self.last_view = None
        self.switch_session(e)
        self.show_map()
        ident = e.meta["identity"]
        line = (f"◆ {'mapped' if fresh else 'known'} {e.name} · {ident.get('class')} · {e.fmt} · {C.human(e.size)}"
                + (f" · {C.num(e.lines)} lines" if e.lines else "")
                + (f" · mapped in {e.meta.get('mapping', {}).get('secs')}s" if fresh else f" · read {e.meta.get('reads')}×"))
        if announce:
            chat(line, "DiagnosticOk")
            chat(f"  memory: {e.edir}", "Comment")
            kl = K.summary_line(e)
            if kl:
                chat("  " + kl + " (===k shows it)", "Special")
            self.note_hist(f"System: the user opened {e.path}; it is now the current file ({e.fmt}).")
            if e.random_access:
                self.view_lines(1, 40)
        self.emit_state()
        return line

    # ----- requests (the LLM loop)
    def note_hist(self, text: str, k: str = "sys"):
        self.history.append({"role": "user", "content": text, "k": k})

    def system_prompt(self) -> str:
        if not self.iface.connected:
            return SYSTEM_PROMPT
        net = self.iface.net
        return SYSTEM_PROMPT + NET_PROMPT.replace("<NAME>", str(net.name)).replace("<NETWORK>", str(net.network or "network"))

    def messages(self, live: str) -> List[dict]:
        out = [{"role": "system", "content": self.system_prompt()}]
        for m in self.history + [{"role": "user", "content": live}]:
            if out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def compact(self, start: int):
        for m in self.history[start:]:
            if m["k"] == "res" and len(m["content"]) > self.args.keep_result:
                m["content"] = m["content"][: self.args.keep_result] + " …[shortened]"
        size = sum(len(m["content"]) for m in self.history)
        dropped = False
        for kinds in (("res", "act"), ("req", "steer", "final", "sys")):
            i = 0
            while size > self.args.history_chars and i < len(self.history) - 2:
                if self.history[i]["k"] in kinds:
                    size -= len(self.history.pop(i)["content"])
                    dropped = True
                else:
                    i += 1
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
        if dropped and not self.trim_told:
            self.trim_told = True
            chat(f"  (conversation beyond {self.args.history_chars} chars: oldest steps dropped from my context; "
                 "file memories are unaffected)", "Comment")

    def live_block(self, text: str, origin, ctx) -> str:
        parts = [f"[TODAY] {time.strftime('%Y-%m-%d %H:%M')}"]
        if self.e:
            parts.append("[CURRENT FILE]\n" + C.render_brief(self.e, self.args.max_brief))
            kb = K.render_llm(self.e, text, self.args.max_knowledge)
            if kb:
                parts.append(kb)
        else:
            parts.append("[CURRENT FILE] none — open one with the open action")
        rows = sorted(C.catalog_load()["files"].items(), key=lambda kv: kv[1].get("last_read", ""), reverse=True)
        others = [f"{p} ({r.get('format')}, {C.human(r.get('size'))}, @{r.get('sha256', '')[:10]})" for p, r in rows[:6]
                  if not self.e or r.get("sha256") != self.e.sha]
        if others:
            parts.append("[OTHER FILES IN MEMORY] " + "; ".join(others))
        if isinstance(ctx, dict) and ctx.get("pane") in ("view", "map") and ctx.get("text"):
            parts.append(f"[USER POSITION] cursor in the {ctx['pane'].upper()} pane on: {clip(ctx.get('text'), 200)}")
        if self.iface.connected:
            parts.append(f"[NETWORK] {self.iface.net.network} as @{self.iface.net.name}; members: {self.iface.net.who()}")
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network ({'private' if origin.get('to') else 'in the room'}). "
                         "Your declare_result reply is sent to them.")
        parts.append(f"The request you are working on: {text}")
        return "System (live state, not a new request):\n" + "\n\n".join(parts)

    def take_steering(self):
        with self.lock:
            steers = list(self.steering)
            self.steering.clear()
        for s in steers:
            chat(f"  ↪ steering: {s.splitlines()[0]}", "Comment")
            self.history.append({"role": "user", "content": f"User Steering Update: {s}", "k": "steer"})

    def run_request(self, task: dict) -> Tuple[str, str]:
        text, origin, sel, ctx = task["text"], task.get("origin"), task.get("sel"), task.get("ctx")
        logw(f"\n▶ REQUEST: {text}", "Title")
        if origin:
            chat("")
            chat(f"🌐 network request #{origin.get('id')} from @{origin.get('from')}: {clip(text, 300)}", "Special")
        who = f"[Network request #{origin.get('id')} from @{origin.get('from')}] " if origin else ""
        body = f"[REQUEST] {who}{text}"
        if sel:
            body += f"\n[SELECTION] {sel.get('file') or '[No Name]'} lines {sel.get('start')}-{sel.get('end')}:\n{clip(sel.get('text'), 3000)}"
        fresh = text.lower().startswith("fresh:")
        if fresh:
            text = text[6:].strip()
        hit = K.exact(self.e, text) if self.e and not fresh and not sel else None
        if hit:  # the same question on the same content: the answer still holds
            K.bump_recall(self.e, hit["id"])
            chat(f"🧠 from memory — {hit['id']}, answered {hit['at'][:16].replace('T', ' ')} on this same content:", "Special")
            chat(f"◆ {hit['answer']}")
            chat("  (start the question with fresh: to analyse it again)", "Comment")
            self.show_view(f"VIEW · knowledge {hit['id']}", K.render_record(self.e, hit["id"]))
            self.history.append({"role": "user", "content": body, "k": "req"})
            self.history.append({"role": "assistant", "k": "final", "content": json.dumps(
                {"action_type": "declare_result", "status": "SUCCESS", "reply": hit["answer"]}, ensure_ascii=False)})
            return "SUCCESS", hit["answer"]
        self.history.append({"role": "user", "content": body, "k": "req"})
        self.req_start, self.req_body = len(self.history), body
        self.req_steps, self.req_findings = [], []
        try:
            status, reply = self.run_task(text, origin, ctx)
        finally:
            self.compact(self.req_start)
            self.req_body = ""
        try:
            self.keep_knowledge(text, reply, status, origin)
        except (Cancelled, C.Cancelled):
            raise
        except Exception as ex:
            logw(f"[KNOWLEDGE ERROR] {type(ex).__name__}: {ex}", "ErrorMsg")
        return status, reply

    def keep_knowledge(self, question: str, answer: str, status: str, origin):
        """Every answered question is kept with its evidence; after an analysis the LLM distills its key findings."""
        e = self.e
        if not e or not question:
            return
        by = f"@{origin.get('from')}" if origin else "user"
        steps = self.req_steps
        fids = [f["id"] for f in self.req_findings if f.get("id")]
        qid = K.add_qa(e, question, answer, status, by, steps, fids)
        if fids:
            K.link(e, qid, fids)
        kept, confirmed = [(f["id"], f["title"]) for f in self.req_findings if f.get("added")], []
        if status == "SUCCESS" and self.args.distill and any(s["kind"] in DATA_ACTIONS for s in steps):
            self.set_phase("keeping knowledge")
            obj = None
            try:
                obj = self.llm.json_call(K.DISTILL_PROMPT, K.distill_input(e, question, answer, steps))
            except Exception as ex:
                logw(f"🧠 distill failed: {type(ex).__name__}: {ex}", "ErrorMsg")
            logw(f"🧠 distill → {json.dumps(obj, ensure_ascii=False)[:1500] if obj else '(nothing)'}", "Comment")
            ev = [f"{s['do']} → {s['got']}" for s in steps if s["kind"] in DATA_ACTIONS]
            for f in K.parse_findings(obj):
                fid, added = K.add_finding(e, f["title"], f["detail"], f["refs"], f["tags"], by="hbmx", qid=qid, evidence=ev)
                (kept if added else confirmed).append((fid, f["title"]))
            K.link(e, qid, [fid for fid, _ in kept + confirmed])
        msg = f"🧠 kept {qid}"
        if kept:
            msg += " · new findings: " + "; ".join(f"{fid} {clip(t, 70)}" for fid, t in kept)
        if confirmed:
            msg += " · confirmed: " + ", ".join(fid for fid, _ in confirmed)
        chat(msg, "Special")
        self.show_map()

    def run_task(self, text: str, origin, ctx) -> Tuple[str, str]:
        bad = 0
        for turn in range(1, self.args.max_turns + 1):
            if self.cancel_ev.is_set():
                raise Cancelled()
            logw(f"── turn {turn} ──", "Comment")
            self.take_steering()
            self.interrupt_ev.clear()
            self.set_phase(f"thinking {turn}/{self.args.max_turns}")
            msgs = self.messages(self.live_block(text, origin, ctx))
            if os.environ.get("HBMX_DEBUG_PROMPT"):
                try:
                    with open(os.environ["HBMX_DEBUG_PROMPT"], "w", encoding="utf-8") as f:
                        for m in msgs:
                            f.write(f"===== {m['role']}\n{m['content']}\n")
                except OSError:
                    pass
            raw, interrupted = self.llm.stream(msgs, self.interrupt_ev)
            if self.cancel_ev.is_set():
                raise Cancelled()
            if interrupted:
                chat("  ⏹ generation interrupted", "Comment")
                self.note_hist("System: the user interrupted this generation turn.", "res")
                continue
            act = parse_llm_json(raw)
            if not act or not (act.get("action_type") or act.get("action") or act.get("tool")):
                bad += 1
                chat("  ⚠ the reply was not one JSON action; asking again", "Comment")
                self.history.append({"role": "assistant", "content": raw[-1500:] or "(empty)", "k": "act"})
                self.note_hist("System: reply with ONLY one JSON object with \"action_type\" (see ACTIONS).", "res")
                if bad >= 4:
                    return "FAILED", "The model did not produce a usable action."
                continue
            act = normalize_action(act)
            kind = str(act.get("action_type") or "").strip()
            self.history.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False), "k": "act"})
            if kind == "declare_result":
                self.history[-1]["k"] = "final"
                status = "SUCCESS" if str(act.get("status") or "SUCCESS").upper() == "SUCCESS" else "FAILED"
                reply = str(act.get("reply") or act.get("reason") or "").strip()
                if len(reply) < 200 and self.req_findings:  # small models put the answer in the finding: show it
                    reply += "".join(f"\n• {f['title']}: {f['detail']}" + (f" (refs: {', '.join(f['refs'])})" if f.get("refs") else "")
                                     for f in self.req_findings)
                chat(f"{'◆' if status == 'SUCCESS' else '✖'} {reply}", None if status == "SUCCESS" else "ErrorMsg")
                return status, reply
            label = " ".join(f"{k}={clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 60)}"
                             for k, v in act.items() if k not in ("action_type", "reason", "code"))
            chat(f"  ⚙ {kind} {label}", "Function")
            if act.get("reason"):
                chat(f"    💭 {clip(act['reason'], 200)}", "Comment")
            self.set_phase(kind)
            try:
                evidence = self.do_action(kind, act, origin)
            except (Cancelled, C.Cancelled):
                raise
            except C.HbmxError as ex:
                evidence = f"Error: {ex}"
            except Exception as ex:
                evidence = f"Error: {type(ex).__name__}: {ex}"
            evidence = str(evidence)
            first = evidence.split("\n", 1)[0]
            self.req_steps.append({"kind": kind, "do": f"{kind} {label}".strip(), "got": clip(first, 200),
                                   "full": O.clip_output(evidence, 900)})
            more = f"  (+{evidence.count(chr(10))} lines in VIEW)" if "\n" in evidence else ""
            chat(f"    → {clip(first, 220)}{more}", "ErrorMsg" if evidence.startswith(("Error", "System: denied")) else "Comment")
            self.note_hist(f"[RESULT of {kind}]\n{O.clip_output(evidence, self.args.max_output)}", "res")
        return "FAILED", f"Stopped after {self.args.max_turns} actions without a result."

    # ----- actions
    def need_file(self) -> C.Entry:
        if not self.e:
            raise C.HbmxError("no file is open: use the open action with a path first")
        return self.e

    def result(self, title: str, text: str) -> str:
        self.show_view(f"VIEW · {title}", text)
        return text

    def do_action(self, kind: str, a: dict, origin) -> str:
        if kind in NET_ACTIONS and not self.iface.connected:
            return "System: denied — hbmx is not on the Intelligence Network (the user joins with hbmxj)."
        ctl = self.ctl()
        if kind == "open":
            path = a.get("path") or a.get("file") or a.get("name")
            line = self.do_open(str(path or ""))
            return line + "\n" + C.render_brief(self.e, self.args.max_brief)
        if kind == "files":
            return self.result("files in memory", C.files(40))
        if kind == "who":
            return self.iface.net.who()
        if kind == "post":
            to = a.get("to") if isinstance(a.get("to"), list) else ([a["to"]] if isinstance(a.get("to"), str) else None)
            return "posted" if self.iface.say(str(a.get("text") or ""), to=to) else "System: not sent."
        if kind == "ask_agent":
            self.set_phase("waiting @" + str(a.get("to")))
            return self.iface.ask(str(a.get("to") or ""), str(a.get("text") or ""), a.get("wait") or self.args.ask_timeout,
                                  self.cancel_ev, re=origin.get("id") if origin else None)
        if kind == "wait_agent":
            self.set_phase("waiting @" + str(a.get("from")))
            return self.iface.wait(str(a.get("from") or ""), a.get("wait") or self.args.ask_timeout, self.cancel_ev)
        e = self.need_file()
        if kind == "map":
            zone, region = a.get("zone"), a.get("region")
            if zone is None and region is None and isinstance(a.get("id"), str):
                zone, region = (a["id"], None) if a["id"].startswith("z") else (None, a["id"])
            text = C.render_map(e, zone=zone, region=region)
            return self.result(f"map {zone or region or ''}", text)
        if kind == "view":
            if a.get("time"):
                text = O.view(e, count=_int(a.get("count"), 30), time_range=str(a["time"]), ctl=ctl)
            elif a.get("byte") is not None:
                text = O.view(e, byte=_int(a.get("byte"), 0), length=_int(a.get("length"), 256))
            else:
                region = a.get("region") or a.get("zone") or a.get("landmark")
                text = O.view(e, line=_int(a.get("line")), count=_int(a.get("count"), 30), region=region, ctl=ctl)
                m = re.match(r"# view lines ([\d,]+)", text)
                if m:
                    self.last_view = {"kind": "lines", "pos": _int(m.group(1)), "count": _int(a.get("count"), 30)}
            return self.result("view", text)
        if kind == "record":
            return self.result("record", O.record(e, _int(a.get("line"), 1)))
        if kind == "seek":
            text = O.seek(e, line=_int(a.get("line")), byte=_int(a.get("byte")), time_=a.get("time"), ctl=ctl)
            return self.result("seek", text)
        if kind == "search":
            rx = a.get("regex")  # true/false; small models sometimes put the pattern there
            pattern = a.get("pattern") or a.get("query") or (rx if isinstance(rx, str) and rx.lower() not in ("true", "false") else "")
            regex = _bool(rx, True) if isinstance(rx, bool) or str(rx).lower() in ("true", "false") else not _bool(a.get("fixed"), False)
            text = O.search(e, str(pattern or ""), regex=regex,
                            icase=_bool(a.get("ignore_case"), False), max_hits=_int(a.get("max"), 20),
                            lines=a.get("lines") or a.get("region") or a.get("zone"), count=_bool(a.get("count")),
                            after_line=_int(a.get("after_line")), ctl=ctl)
            return self.result("search", text)
        if kind == "filter":
            where = a.get("where") or a.get("conditions") or a.get("condition")
            text = O.filter_records(e, where, a.get("fields"), _int(a.get("max"), 20), a.get("lines") or a.get("zone"),
                                    _bool(a.get("count"), False), ctl=ctl)
            return self.result("filter", text)
        if kind == "stats":
            text = O.stats(e, str(a.get("kind") or a.get("type") or "levels"), field=a.get("field"), regex=a.get("regex"),
                           exact=_bool(a.get("exact")), lines=a.get("lines") or a.get("zone") or a.get("region"),
                           top=_int(a.get("top"), 20), where=a.get("where"), baseline=a.get("baseline"), ctl=ctl)
            return self.result(f"stats {a.get('kind')}", text)
        if kind == "extract":
            text = O.extract(e, a.get("name"), pattern=a.get("pattern"), regex=not _bool(a.get("fixed"), False),
                             icase=_bool(a.get("ignore_case"), False), lines=a.get("lines"), time_range=a.get("time"),
                             where=a.get("where"), ctl=ctl)
            self.show_map()
            return self.result("extract", text)
        if kind == "inflate":
            est = (e.meta.get("structure") or {}).get("uncompressed") or e.size * 6
            if est > 2 << 30 and not self.approve(f"inflating {e.name} to about {C.human(est)} in memory"):
                return "System: denied by the user."
            text, inner = O.inflate(e, ctl)
            self.e = inner
            self.show_map()
            return self.result("inflate", text + "\n(the inflated copy is now the current file)")
        if kind == "recall":
            return self.result("recall", K.recall(e, str(a.get("query") or a.get("text") or a.get("topic") or "")))
        if kind == "functions":
            return self.result("functions", O.functions(e))
        if kind == "write_function":
            code = a.get("code") or a.get("script") or ""
            text = O.write_function(e, a.get("name"), a.get("lang") or a.get("language") or "bash", code,
                                    a.get("description") or "", a.get("usage") or a.get("name") or "")
            self.show_view(f"VIEW · function {a.get('name')}", O.function_source(e, str(a.get("name")).lower()))
            self.show_map()
            return text
        if kind == "run_function":
            name = str(a.get("name") or "").lower()
            args = a.get("args") or []
            args = args if isinstance(args, list) else str(args).split()
            f = O._fn(e, name)
            if f.get("by") != "hbmx":
                self.show_view(f"VIEW · function {name}", O.function_source(e, name))
                if not self.approve(f"run function {name} [{f.get('by')}] {' '.join(map(str, args))}"):
                    return "System: denied — the user did not allow running it."
            rc, out, secs = O.run_function(e, name, args, timeout=_int(a.get("timeout"), self.args.fn_timeout), ctl=ctl)
            head = f"# run {name} {' '.join(map(str, args))} · rc={rc} · {secs}s"
            return self.result(f"run {name}", head + "\n" + out)
        if kind == "shell":
            cmd = str(a.get("cmd") or a.get("command") or "").strip()
            if not cmd:
                return "Error: shell needs cmd"
            if not self.approve(f"shell: {cmd}"):
                return "System: denied — the user did not allow the command."
            return self.result("shell", self.shell(cmd))
        if kind == "note":
            refs = a.get("refs")
            refs = refs if isinstance(refs, list) else ([refs] if refs else [])
            nkind = str(a.get("kind") or ("landmark" if a.get("label") else "finding" if a.get("title") else "howto"))
            if nkind == "finding" and a.get("title"):
                fid, added = K.add_finding(e, str(a["title"]), str(a.get("detail") or ""), refs,
                                           tags=[a["severity"]] if a.get("severity") else [], by="hbmx-llm")
                self.req_findings.append({"id": fid, "added": added, "title": clip(a.get("title"), 160),
                                          "detail": clip(a.get("detail"), 600), "refs": [str(r) for r in refs][:6]})
                self.show_map()
                return f"# finding {fid} {'saved' if added else 'already known: confirmed again'}: {clip(a['title'], 120)}"
            text = O.note(e, nkind,
                          by="hbmx-llm", label=a.get("label") or "", line=_int(a.get("line")), byte=_int(a.get("byte")),
                          time_=a.get("time"), title=a.get("title") or "", detail=a.get("detail") or "", refs=refs,
                          severity=a.get("severity") or "", text=a.get("text") or "")
            self.show_map()
            return text
        return f"Error: unknown action_type '{kind}'. Use one of: {', '.join(ACTIONS)}"

    def shell(self, cmd: str) -> str:
        e = self.e
        env = dict(os.environ, HBMX_FILE=e.path or "", HBMX_SHA=e.sha, HBMX_DATA=e.ddir, HBMX_META=e.meta_file,
                   HBMX_CORE=C.CORE, HBMX_PYTHON=C.PYTHON)
        os.makedirs(e.ddir, exist_ok=True)
        t0 = time.time()
        p = subprocess.Popen(["bash", "-c", cmd], cwd=e.ddir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        buf = bytearray()

        def pump():
            for chunk in iter(lambda: p.stdout.read(65536), b""):
                if len(buf) < 1 << 20:
                    buf.extend(chunk)
                else:
                    p.kill()
                    break
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        while p.poll() is None:
            if self.cancel_ev.is_set():
                p.kill()
                raise Cancelled()
            if time.time() - t0 > self.args.fn_timeout:
                p.kill()
                buf.extend(f"\n[hbmx: killed after {self.args.fn_timeout}s]".encode())
                break
            time.sleep(0.05)
        th.join(2)
        return f"# shell rc={p.wait()} · {time.time() - t0:.1f}s · $ {clip(cmd, 160)}\n" + buf.decode("utf-8", "replace")


# ---------------------------------------------------------------- main
def reader(agent: Agent):
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        t = msg.get("t")
        if t == "input":
            agent.on_input(msg.get("text", ""), msg.get("ctx"), msg.get("sel"))
        elif t == "open":
            agent.open_request(str(msg.get("path") or ""))
        elif t == "nav":
            agent.navq.put(msg)
        elif t == "net":
            op = msg.get("op")
            if op == "join":
                agent.iface.join(msg.get("name"), msg.get("purpose"), msg.get("hub"), msg.get("network"))
            elif op == "leave":
                agent.iface.leave()
            elif op == "say":
                agent.iface.say(msg.get("text", ""), to=msg.get("to"))
        elif t == "cancel":
            agent.cancel()
        elif t == "interrupt":
            agent.interrupt()
        elif t == "quit":
            break
    agent.shutdown()
    agent.iface.net.close()
    emit({"t": "exit"})


def main():
    global ARGS
    if sys.stdin.isatty():
        print("hbmx.py is the backend of the Neovim UI. Inside nvim:  :source ~/echo-home/.vim/mimd/hbmx/hbmx.lua\n"
              "From a shell use the engine directly:  hbmx_core.py probe <file>", file=sys.stderr)
    p = argparse.ArgumentParser(description="hbmx: large-file reader and data analyst agent (backend of hbmx.lua)")
    p.add_argument("--port", type=int, default=8080, help="port of the local LLM server")
    p.add_argument("--host", default="localhost")
    p.add_argument("--open", default="", help="file to open at start")
    p.add_argument("--cwd", default=os.getcwd(), help="folder relative paths are read from")
    p.add_argument("--auto", action="store_true", help="auto-approve shell and the agent's own functions (also autopilot.yaml hbmx.allow)")
    p.add_argument("--max-turns", type=int, default=20, help="actions per request")
    p.add_argument("--max-output", type=int, default=3500, help="chars of one action result fed to the LLM (VIEW shows all)")
    p.add_argument("--max-brief", type=int, default=2600, help="chars of the current file's map shown to the LLM each turn")
    p.add_argument("--max-knowledge", type=int, default=1500, help="chars of the file's knowledge shown to the LLM each turn")
    p.add_argument("--no-distill", dest="distill", action="store_false",
                   help="do not extract key findings with an extra LLM call after each analysed question")
    p.add_argument("--history-chars", type=int, default=24000, help="conversation kept in context (LLM n_ctx 16k → ~24k chars)")
    p.add_argument("--keep-result", type=int, default=600, help="chars kept of an action result once its request is over")
    p.add_argument("--fn-timeout", type=int, default=180, help="seconds a function or shell command may run")
    p.add_argument("--pane-lines", type=int, default=3000, help="lines a pane shows at most")
    p.add_argument("--ask-timeout", type=int, default=180)
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("HBMX_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    ARGS = p.parse_args()

    agent = Agent(ARGS)
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.navigator, daemon=True).start()
    llm_ok = agent.llm.ensure()
    chat(f"◆ hbmx · large-file reader & data analyst · {os.path.basename(agent.llm.model or '') or 'no LLM'}", "Title")
    if not llm_ok:
        chat(f"  cannot reach the LLM at {agent.llm.base} — start it; I retry on each request (maps and panes work without it)",
             "WarningMsg")
    chat(f"  memory {C.HOME} · :HbmxOpen <file> or ===o <file> · ===h help", "Comment")
    if agent.auto:
        chat("  auto-approve is ON (shell / functions I write)", "WarningMsg")
    logw("LLM — raw LLM output, streamed as it is generated", "Title", quiet=True)
    ilog("IFACE — Intelligence Network traffic and decisions", "Title", quiet=True)
    import hbmx_iface
    if hbmx_iface.INET_ERROR:
        ilog("⚠️ Intelligence Network driver not loaded; hbmx works locally but cannot join.", "ErrorMsg")
        ilog("   " + hbmx_iface.INET_ERROR, "ErrorMsg")
    else:
        ilog(f"   inet driver: {hbmx_iface.INET_DIR}", "Comment", quiet=True)
    if ARGS.open:
        agent.open_request(ARGS.open)
    if ARGS.net_name:
        agent.iface.join()
    agent.emit_state()
    reader(agent)


if __name__ == "__main__":
    main()
