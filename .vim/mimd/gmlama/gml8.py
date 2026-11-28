#!/usr/bin/env python
"""
gml8: the operator of this machine's llama.cpp LLM services, and an agent shell powered by them. Headless backend of
gml8.lua (same folder); the engine is gml8_core.py, the network interface gml8_iface.py.

It keeps the inventory (gml.<hostname>.yson) in step with the model series (merge at start), launches entries detached
(they outlive gml8 and Neovim), tracks every run and probes it through the OpenAI API (/v1/models), reads the servers'
logs (speed, errors), diagnoses crashes, heals a service that died after it was up, relaunches an entry with changed
params, publishes the services on the discovery bus (gml8bus.lua, run/services.json) and powers its own LLM from the
first service that answers (before that it works without an LLM: launches, stops, probes, edits).

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"w","b":"chat"|"iface"|"log","text":..,"hl":..} · {"t":"w","b":"log","text":..,"raw":true} · {..,"eos":true}
  {"t":"state","busy":b,"phase":..,"queue":n,"auto":b,"approval":..,"net":{..},"power":{..}|null,"counts":{..},
   "inventory":path,"launched":n (gml8's own runs that are running),"think":b,"selected":key}
  {"t":"inv","marks":{"<line>":{"text":..,"hl":..,"sign":..,"line_hl":..}},"path":..}     the inventory's marks
  {"t":"idx","key":..,"line":n,"title":..,"lines":[..],"hls":[[row,cs,ce,group]..],"stage":[..]|absent,
   "log":path|null,"active":b,"staged":b,"run":"⚡ :8090"|null}                          MODEL IDX (+ STAGE, LOG)
  {"t":"services","list":[..]}                                                          the discovery bus
  {"t":"provided","rid":..,"service":{..}|null,"launching":{..}|null,"error":..}         answer to a bus request
  {"t":"req","id":N,"op":"context"|"ex"|"lua"|"reload",..} · {"t":"show"} · {"t":"exit"} · {"t":"alert","text":..}
nvim -> backend
  {"t":"input","text":..,"ctx":{..}}
  {"t":"op","op":"select","line":n,"text":<the buffer's line>,"modified":b}
  {"t":"op","op":"stage","key":..,"line":n,"text":<the STAGE buffer>}  ·  {"t":"op","op":"unstage","key":..}
  {"t":"op","op":"launch"|"relaunch"|"stop"|"commit","line":n,"key":..,"stage":<text>|null,"text":..,"modified":b}
  {"t":"op","op":"stop","port":n} · {"t":"op","op":"merge"|"probe"|"hw"} · {"t":"op","op":"provide","rid":..,"want":{..}}
  {"t":"res","id":N,..} · {"t":"net",..} · {"t":"cancel"} · {"t":"quit"}
"""
import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from typing import List, Optional

_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")  # LF to Neovim, on Windows too
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

import gml8_core as core  # noqa: E402
from gml8_core import ACTIVE, ICON, Entry, clip, gib, tilde, utc_now, when  # noqa: E402

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
    emit({"t": "w", "b": "log", "text": text, "hl": hl})


def parse_llm_json(raw: str) -> Optional[dict]:
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


# ---------------------------------------------------------------- LLM (powered by a service gml8 runs)
THINK_HL = "Gml8Think"


class LLM:
    def __init__(self, think=False):
        self.port = None
        self.model = None
        self.client = None
        self.think = think
        self.label = ""
        self.host = "127.0.0.1"

    @property
    def base(self):
        return f"http://{self.host}:{self.port}/v1" if self.port else "(no service)"

    def target(self, port: Optional[int], model: str = "", label: str = "", host: str = "127.0.0.1"):
        if port == self.port and host == self.host and self.client:
            self.label = label or self.label
            return
        self.port, self.host, self.model, self.label = port, host, (model or None), label
        self.client = None
        if port:
            from openai import OpenAI
            self.client = OpenAI(base_url=self.base, api_key="gml8", timeout=600)

    def ready(self) -> bool:
        if not self.client:
            return False
        if self.model:
            return True
        try:
            models = self.client.models.list()
            self.model = models.data[0].id if models.data else None
        except Exception:
            self.model = None
        return bool(self.model)

    def _kw(self):
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": bool(self.think)}}}

    def stream(self, messages, cancel: threading.Event) -> str:
        if not self.ready():
            raise RuntimeError("no LLM service is up to power gml8")
        resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.0, stream=True,
                                                   **self._kw())
        out, buf, last, mode, tag = "", "", time.time(), "text", False

        def flush():
            nonlocal buf, last
            if buf:
                emit({"t": "w", "b": "log", "text": buf, "raw": True, "hl": THINK_HL if mode == "think" else None})
                buf, last = "", time.time()

        def put(text, m):
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
                while txt:
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
                try:
                    resp.close()
                except Exception:
                    pass
            emit({"t": "w", "b": "log", "text": "", "eos": True})
        return out

    def json_call(self, system: str, user: str) -> Optional[dict]:
        if not self.ready():
            raise RuntimeError("no LLM service is up")
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}], **self._kw())
        content = r.choices[0].message.content or ""
        think = getattr(r.choices[0].message, "reasoning_content", None) or getattr(r.choices[0].message, "reasoning", None) or ""
        logw("── 🌐 network attention ──", "Comment")
        if think.strip():
            logw("💭 " + think.strip()[-3000:], THINK_HL)
        logw((content.strip()[-1500:] or "(empty)") + "\n", "Comment")
        return parse_llm_json(content)


# ---------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You are gml8, the operator of the llama.cpp LLM services of the user's machine. You work in the user's Neovim (the gml8 tab) and, when joined, on the Intelligence Network. You are powered by one of the services you run; other agent shells (mksk …) discover the services that are up through gml8's bus.

THE INVENTORY (the user's gml.<host>.yson), one entry per line:
  <model file>: ["<ACCEL>/<profile name>", "<llama-server params>", "<llama.cpp version>", "<gpu device>"]
ACCEL GPU = the GPU build of llama.cpp, none/CPU = the CPU build.
- RESERVED lines (the top, up to the first ---- SECTION ---- line): line N is slot N of the model series. "<model>: []" is a placeholder (gml8 gives it a profile, -c 8192 and a free port at its first launch). Reserved entries are OPEN: change any of their params to fit this machine (port, context, KV cache types, GPU layers, device).
- NAMED entries (sections after ---- NAMED ----): the user's own recipes, addressed by @name, may carry {env, script, pre, post}. They are launched as written; change them only when the user asks.
- Refer to entries as #<line> (reserved) or @<name> (named).
[SERVICES] is what runs: state (launching, loading, up, stalled, crashed, failed, stopped; foreign = a llama-server started outside gml8), port, model, n_ctx, speed.

RULES
1. ONE action per turn, then read its result. Base every claim on results and the context, never on guesses.
2. To bring a model up: launch its entry. To change how a running one works: relaunch with "set" (flags to change, e.g. {"-c":"16384"}) and/or "unset" (flags to drop). A relaunch stops the running service first.
3. Look at the capacity (entry) before launching a big model and say when it will not fit; prefer fixes that keep the model: a smaller -c, a quantized KV cache (-ctk q8_0 -ctv q8_0), fewer -ngl, -fit on.
4. A crash or a failed launch: diagnose first (log rules + capacity), name the cause plainly, then fix it with relaunch (reserved entries) or propose the change (named entries).
5. Do only what the request asks. A question gets read-only actions (status, entries, entry, logs, diagnose, history, hardware, models) and then declare_result. Change something (launch, relaunch, stop, set_params, commit, note, ask_agent) only when the request asks for that change or for a fix. Say so before you touch a foreign service. launch, relaunch, stop, commit, local and editor actions ask the user.
6. Staged changes (set_params, relaunch) are not in the inventory until commit writes the entry's line.
7. Finish with declare_result as soon as the results answer the request: "reply" = the concrete answer (entries, ports, states, causes, what changed, what remains). Never repeat an action whose result you already have.
8. Output ONLY one JSON object with only the fields the action needs (an action never carries results).

ACTIONS — "action_type" is exactly one of:
  {"action_type":"status"}                          the services and every entry with a run
  {"action_type":"entries","query":"e4b"}           inventory lines (query optional)
  {"action_type":"entry","ref":"#11"}               one entry in depth: model (GGUF), params, capacity, run, history
  {"action_type":"launch","ref":"#11"}              start it (detached) and wait until it answers
  {"action_type":"relaunch","ref":"#11","set":{"-c":"16384"},"unset":["-mm"]}   stop + start with changed params
  {"action_type":"stop","ref":"#11"}                or "port"
  {"action_type":"logs","ref":"#11","lines":40}     the server's output
  {"action_type":"diagnose","ref":"#11"}            why it failed or is slow: log rules, capacity, history
  {"action_type":"set_params","ref":"#11","set":{"-c":"16384"},"unset":["-mm"]}   stage a change (MODEL IDX shows it)
  {"action_type":"commit","ref":"#11"}              write the staged change into the entry's line
  {"action_type":"hardware"}                        RAM, GPUs, their free memory
  {"action_type":"models","query":"gemma"}          model files on disk
  {"action_type":"history","ref":"#11"}             past launches, crashes, notes
  {"action_type":"note","ref":"#11","text":".."}    keep a fact about an entry
  {"action_type":"local","command":".."}            a shell command on this machine
  {"action_type":"run_command","command":"<Ex command>"}
  {"action_type":"who"}  {"action_type":"ask_agent","to":"mksk","text":"..","wait":120}  {"action_type":"post","text":".."}
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
Every action also takes "reason": one short sentence why."""

ACTION_NAMES = ["status", "entries", "entry", "launch", "relaunch", "stop", "logs", "diagnose", "set_params", "commit",
                "hardware", "models", "history", "note", "local", "run_command", "who", "ask_agent", "wait_agent",
                "post", "declare_result"]
ACTION_ALIASES = {"start": "launch", "run": "launch", "restart": "relaunch", "kill": "stop", "log": "logs",
                  "tail": "logs", "troubleshoot": "diagnose", "inspect": "entry", "show": "entry", "list": "entries",
                  "inventory": "entries", "services": "status", "hw": "hardware", "capacity": "entry", "stage": "set_params",
                  "edit": "set_params", "set": "set_params", "write": "commit", "save": "commit", "shell": "local",
                  "result": "declare_result", "finish": "declare_result", "answer": "declare_result", "files": "models"}

HELP_TEXT = """gml8 — the operator of this machine's llama.cpp services (and an agent shell powered by them)
  <text>  a request: "bring up gemma E2B", "why did #11 crash?", "give #1 a 16k context", "what fits on the GPU?"
  ===w <ref> launch · ===R <ref> relaunch (with its STAGE) · ===k <ref|port> stop · ===l the services · ===m merge the series
  ===L [ref|port] power gml8 from that service (alone: back to automatic) · ===hw hardware again · ===r probe now
  ===Lf <port|host:port> power gml8 from a service gml8 does not run (another launcher, a tunnel), touching no entry
  ===s <words> search events · ===a auto-approve · ===T LLM thinking on/off · ==== reset the context · ===x cancel
  ===n network   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
  <ref>: 11 / #11 (a reserved line) · @MBG4_MMDI_disp (a named entry) · part of a model file name
  INVENTORY: the cursor picks the entry for MODEL IDX (unsaved edits of the line show there too) · <CR> MODEL IDX
  MODEL IDX: L launch (the STAGE when it differs) · R relaunch · x stop · C commit the STAGE · u reset it · ss show/edit it ·
             e the entry's line · l log on/off · y yank the command · a ask about it · r refresh
  <Tab> next pane · <S-Tab> previous · i input · <C-c> cancel · q back to your tab"""


def normalize_action(act: dict) -> dict:
    kind = str(act.get("action_type") or act.get("action") or "").strip()
    kind = ACTION_ALIASES.get(kind, kind)
    if kind in ACTION_NAMES:
        return dict(act, action_type=kind)
    for name in ACTION_NAMES:
        if name in act and isinstance(act[name], dict):
            out = {k: v for k, v in act.items() if k not in (name, "action_type", "action")}
            out.update(act[name])
            out["action_type"] = name
            return out
    return act


# ---------------------------------------------------------------- stage (the editable form of an entry)
def stage_lines(e: Entry, header: bool = True) -> List[str]:
    out = []
    if header:
        out.append(f"# {e.key} · {'reserved slot ' + str(e.line) if e.reserved else 'named · ' + e.section} · "
                   "edit, MODEL IDX follows · C commit · L launch · R relaunch · u reset")
    out += [f"model:   {e.model}", f"profile: {e.profile}", f"version: {e.version}", f"device:  {e.device}", "params:"]
    for f, v in core.parse_params(e.params):
        out.append("  " + " ".join(x for x in (f, core.quote(v) if v is not None else None) if x))
    if not e.reserved:
        out.append("env:")
        for k, v in ((e.extra.get("env") or {}).items() if isinstance(e.extra.get("env"), dict) else []):
            out.append(f"  {k}={v}")
        for k in ("script", "pre", "post", "port", "wait"):
            out.append(f"{k + ':':8s} {e.extra.get(k, '') if e.extra.get(k) is not None else ''}".rstrip())
    return out


def parse_stage(text: str) -> dict:
    vals = {"model": "", "profile": "", "version": "", "device": ""}
    params, env, extra, cur = [], {}, {}, None
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([a-z]+):\s*(.*)$", line)
        if m and not line[:1].isspace():
            k, v = m.group(1), m.group(2).strip()
            cur = k
            if k == "params":
                if v:
                    params.append(v)
            elif k == "env":
                if "=" in v:
                    a, b = v.split("=", 1)
                    env[a.strip()] = b.strip()
            elif k in vals:
                vals[k] = v
            elif k in ("script", "pre", "post", "port", "wait") and v:
                extra[k] = int(v) if k in ("port", "wait") and v.isdigit() else v
            continue
        if cur == "params":
            params.append(s)
        elif cur == "env" and "=" in s:
            a, b = s.split("=", 1)
            env[a.strip()] = b.strip()
    if env:
        extra["env"] = env
    vals["params"] = " ".join(params)
    vals["extra"] = extra
    return vals


def norm_params(p: str) -> str:
    return core.join_params(core.parse_params(p or ""))


def stage_diff(e: Entry, v: dict) -> List[str]:
    """What the staged values change, in words."""
    out = []
    for k in ("model", "profile", "version", "device"):
        if (v.get(k) or "") != (getattr(e, k) or ""):
            out.append(f"{k} {getattr(e, k) or '∅'} → {v.get(k) or '∅'}")
    a, b = core.parse_params(e.params), core.parse_params(v.get("params") or "")
    names = []
    for f, _ in a + b:
        n = core.ALIAS.get(f, f) if f else None
        if n and n not in names:
            names.append(n)
    for n in names:
        x, y = core.pget(a, n), core.pget(b, n)
        if x != y:
            flag = next((f for f, _ in b + a if f and core.ALIAS.get(f, f) == n), n)
            out.append(f"+{flag}" + (f" {y}" if y is not True else "") if x is None else f"−{flag}" if y is None
                       else f"{flag} {x if x is not True else ''} → {y if y is not True else ''}".replace("  ", " "))
    if not e.reserved and (v.get("extra") or {}) != (e.extra or {}):
        out.append("env/script/pre/post changed")
    return out


# ---------------------------------------------------------------- MODEL IDX
class Doc:
    """Lines with highlight spans (byte columns, for nvim extmarks)."""

    def __init__(self):
        self.lines: List[str] = []
        self.hls: List[list] = []

    def add(self, *parts):
        row, col, out = len(self.lines), 0, []
        for p in parts:
            text, hl = (p, None) if isinstance(p, str) else p
            text = str(text)
            b = len(text.encode("utf-8"))
            if hl and b:
                self.hls.append([row, col, col + b, hl])
            out.append(text)
            col += b
        self.lines.append("".join(out))

    def head(self, title: str):
        self.add(("─ " + title + " " + "─" * max(3, 58 - len(title)), "Gml8Head"))

    def kv(self, label: str, *parts):
        self.add((f" {label:<9s}", "Gml8Label"), *parts)


MARK = {"ok": ("✔", "Gml8Ok"), "warn": ("⚠", "Gml8Warn"), "bad": ("✘", "Gml8Bad")}
STATE_HL = {"up": "Gml8Up", "loading": "Gml8Loading", "launching": "Gml8Loading", "stalled": "Gml8Warn",
            "crashed": "Gml8Bad", "failed": "Gml8Bad", "stopped": "Gml8Dim", "stopping": "Gml8Dim", "exited": "Gml8Dim"}


def param_parts(pairs, names, changed=()):
    parts = []
    for f, v in pairs:
        if not f or core.ALIAS.get(f, f) not in names:
            continue
        n = core.ALIAS.get(f, f)
        hl_f = "Gml8Staged" if n in changed else "Gml8Flag"
        if parts:
            parts.append(("  ", None))
        parts.append((f, hl_f))
        if v is not None:
            parts.append((" " + ("***" if n in core.SECRET else tilde(str(v))), "Gml8Staged" if n in changed else "Gml8Arg"))
    return parts


# ---------------------------------------------------------------- agent
class Cancelled(Exception):
    pass


class Agent:
    def __init__(self, args):
        self.args = args
        self.mem = core.Memory(args.memory)
        self.inv = core.Inventory(args.inventory, args.series)
        self.hw = core.Hardware.shared()
        self.runs = core.Runs(self.mem)
        self.llm = LLM(args.think)
        self.running = True
        self.tasks: "queue.Queue" = queue.Queue()
        self.ops: "queue.Queue" = queue.Queue()
        self.pending = 0
        self.current = None
        self.phase = "idle"
        self.cancel = threading.Event()
        self.auto = args.auto or autopilot_allows()
        self.approval = None
        self.recent: deque = deque(maxlen=6)
        self.reqs, self.req_id, self.rlock = {}, 0, threading.Lock()
        self.wake = threading.Event()
        self.tick_lock = threading.Lock()
        self.launch_lock = threading.RLock()
        self.ticks = 0
        self.power_id = None
        self.power_pref = args.llm or ""
        self.power_force = None  # ===Lf: {"host", "port"} — an external service powers gml8, no entry or run involved
        self.power_waiting_said = False
        self.sel = None  # {"line", "key", "text", "modified"}
        self.stages = {}  # key -> STAGE text (what the user staged)
        self.last_inv = self.last_idx = self.last_services = None
        self.heals = {}  # key -> [times]
        self.provides = []  # pending bus requests: {"rid", "want", "key", "until"}
        from gml8_iface import IFace
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
        c = {}
        for r in self.runs.active():
            c[r["state"]] = c.get(r["state"], 0) + 1
        return c

    def power_info(self) -> Optional[dict]:
        f = self.power_force
        if f:
            return {"key": "ext", "port": f["port"], "host": f["host"], "model": self.llm.model or "", "pref": "",
                    "external": True} if self.llm.port else None
        r = self.runs.runs.get(self.power_id) if self.power_id else None
        if not r or not self.llm.port:
            return None
        return {"key": r.get("key"), "port": r.get("port"), "model": self.llm.model or "", "pref": self.power_pref}

    def emit_state(self):
        emit({"t": "state", "busy": self.busy, "phase": self.phase, "queue": self.pending, "auto": self.auto,
              "approval": self.approval["text"] if self.approval else None, "net": self.iface.summary(),
              "power": self.power_info(), "counts": self.counts(), "inventory": self.inv.path,
              "launched": sum(1 for r in self.runs.active() if r.get("origin") == "gml8"),
              "think": bool(self.llm.think), "selected": (self.sel or {}).get("key")})

    def set_phase(self, p):
        self.phase = p
        self.emit_state()

    def dash_brief(self) -> str:
        c = self.counts()
        return ", ".join(f"{n} {k}" for k, n in c.items()) or "no service running"

    def net_table(self) -> str:
        return "\n".join(self.run_line(r) for r in self.runs.active()) or "(no service running)"

    # ----- the live picture
    def watcher(self):
        while self.running:
            try:
                self.tick()
            except Exception as e:
                logw(f"watch error: {type(e).__name__}: {e}", "ErrorMsg")
            self.wake.wait(self.args.interval)
            self.wake.clear()

    def tick(self):
        with self.tick_lock:
            self.ticks += 1
            self.inv.load()
            if self.ticks % 4 == 1:
                for r in self.runs.adopt(core.llama_processes(), self.inv):
                    chat(f"🔀 found llama-server pid {r['pid']} on :{r['port']} (started outside gml8) → "
                         f"{r['key']} {os.path.basename(str(r['model']))}", "Comment")
            for r in list(self.runs.active()):
                self.update_run(r)
            self.runs.save()
            self.power_tick()
            self.provide_tick()
        self.emit_inv()
        self.publish()
        self.emit_idx()
        self.emit_state()

    def update_run(self, r: dict):
        now = time.time()
        if not self.runs.alive(r):
            if r.get("stop_requested") or r.get("state") == "stopping":
                r["state"] = "stopped"
                return
            lines = core.log_tail(r.get("log"), 400)
            facts = core.log_facts(lines)
            was_up = bool(r.get("ready_at"))
            r["ended"] = utc_now()
            r["diag"] = facts["errors"]
            ran = int(now - (r.get("t0") or now))
            if r.get("origin") == "foreign":
                r["state"] = "exited"
                chat(f"⏹ llama-server pid {r['pid']} on :{r['port']} ({r['key']}, started outside gml8) is gone", "Comment")
                self.mem.event("gone", key=r["key"], port=r["port"], pid=r["pid"])
                return
            r["state"] = "crashed" if was_up else "failed"
            code = r.get("code")
            why = facts["errors"][0] if facts["errors"] else None
            chat(f"{ICON[r['state']]} {r['key']} :{r['port']} {'exited' if was_up else 'failed to start'}"
                 + (f" (exit {code})" if code is not None else "") + f" after {ran} s"
                 + (f": {why['cause']}" if why else ""), "ErrorMsg")
            if why:
                chat(f"   evidence: {why['evidence']}", "Comment")
                chat(f"   fix: {why['fix']}", "Comment")
            else:
                tail = [l for l in lines[-6:] if l.strip()]
                if tail:
                    chat("   log: " + clip(" ⏎ ".join(tail), 400), "Comment")
            self.mem.event("crash" if was_up else "fail", key=r["key"], port=r["port"], pid=r["pid"], code=code,
                           secs=ran, cause=why["kind"] if why else None, evidence=why["evidence"] if why else None)
            self.mem.update_entry(r["key"], lambda d: (d.update(fails=d.get("fails", 0) + 1, last_fail=utc_now()),
                                                       d.setdefault("failures", []).append(
                                                           {"at": utc_now(), "cause": why["cause"] if why else f"exit {code}",
                                                            "params": r.get("params")}),
                                                       d.update(failures=d["failures"][-8:])))
            emit({"t": "alert", "text": f"gml8: {r['key']} :{r['port']} {r['state']}" + (f" — {why['cause']}" if why else "")})
            self.after_crash(r, was_up, why)
            return
        pr = core.probe(r["port"], self.args.probe_timeout)
        r["probe"] = pr
        if r.get("log"):
            facts = core.log_facts(core.log_tail(r["log"], 160))
            if facts["tps_gen"]:
                r["tps_gen"] = facts["tps_gen"]
            if facts["tps_prompt"]:
                r["tps_prompt"] = facts["tps_prompt"]
        st = pr["state"]
        if st == "up":
            r["bad_since"] = None
            if r.get("state") != "up":
                first = not r.get("ready_at")
                r["state"] = "up"
                if first:
                    r["ready_at"] = utc_now()
                    r["load_secs"] = int(now - (r.get("t0") or now))
                props = core.server_props(r["port"])
                r["model_id"] = pr.get("model") or r.get("model_id")
                r["n_ctx"] = props.get("n_ctx") or r.get("n_ctx")
                r["vision"] = bool(props.get("vision")) or r.get("vision")
                r["build"] = props.get("build") or ""
                if first:
                    chat(f"✅ {r['key']} up on :{r['port']}" + (f" after {r['load_secs']} s" if r.get('origin') == 'gml8' else "")
                         + f" · {os.path.basename(str(r.get('model')))}" + (f" · n_ctx {r['n_ctx']}" if r.get('n_ctx') else "")
                         + (" · vision" if r.get("vision") else ""), "DiagnosticOk")
                    self.mem.event("up", key=r["key"], port=r["port"], pid=r["pid"], secs=r.get("load_secs"),
                                   n_ctx=r.get("n_ctx"), origin=r.get("origin"))
                    if r.get("origin") == "gml8":
                        self.mem.update_entry(r["key"], lambda d: (d.update(ups=d.get("ups", 0) + 1, last_up=utc_now()),
                                                                   d.setdefault("load_secs", []).append(r.get("load_secs")),
                                                                   d.update(load_secs=d["load_secs"][-6:])))
                    if r.get("post"):
                        threading.Thread(target=self.run_post, args=(r,), daemon=True).start()
                else:
                    chat(f"✅ {r['key']} :{r['port']} answers again", "DiagnosticOk")
        elif st == "loading":
            r["state"] = "loading"
        else:
            if r.get("state") == "up":
                r["bad_since"] = r.get("bad_since") or now
                if now - r["bad_since"] > self.args.stall_after:
                    r["state"] = "stalled"
                    chat(f"⌛ {r['key']} :{r['port']} stopped answering ({st}) for {int(now - r['bad_since'])} s", "WarningMsg")
                    self.mem.event("stall", key=r["key"], port=r["port"], probe=st)
            elif r.get("state") in ("launching", "loading") and now - (r.get("t0") or now) > float(r.get("wait") or 300):
                r["state"] = "stalled"
                chat(f"⌛ {r['key']} :{r['port']} still does not answer after {int(now - r['t0'])} s ({st})", "WarningMsg")

    def run_post(self, r):
        try:
            out = subprocess.run(r["post"], shell=True, capture_output=True, text=True, timeout=120,
                                 env=dict(os.environ, GML8_PORT=str(r["port"]), GML8_KEY=r["key"]))
            chat(f"   post of {r['key']}: exit {out.returncode} {clip(out.stdout + out.stderr, 200)}", "Comment")
        except Exception as e:
            chat(f"   post of {r['key']} failed: {e}", "ErrorMsg")

    def after_crash(self, r, was_up, why):
        e = self.inv.by_key(r["key"])
        if was_up and self.args.heal and e and not (why and why["kind"] in ("args", "model", "version", "bin")):
            times = [t for t in self.heals.get(r["key"], []) if time.time() - t < 3600]
            if len(times) < 3 and time.time() - (r.get("t0") or 0) > 60:
                times.append(time.time())
                self.heals[r["key"]] = times
                chat(f"   ♻ healing: relaunching {r['key']} ({len(times)}/3 this hour)", "Comment")
                self.mem.event("heal", key=r["key"], port=r["port"])
                self.ops.put({"op": "launch", "key": r["key"], "origin": "heal"})
                return
        if self.args.troubleshoot and e and (self.llm.ready() or len(self.ups()) > 0):
            lines = "\n".join(core.log_tail(r.get("log"), 30))
            self.submit(f"[gml8 watch] {r['key']} :{r['port']} {r['state']} "
                        f"({'after running' if was_up else 'while starting'}). Diagnose it and fix it if it is a reserved "
                        f"entry (relaunch with changed params), else propose the change.\nLog tail:\n{clip(lines, 1500)}",
                        None, {"auto": True})

    def ups(self) -> List[dict]:
        return [r for r in self.runs.active() if r.get("state") == "up" and r.get("kind", "chat") == "chat"]

    def force_tick(self):
        """===Lf: gml8's LLM is whatever answers the OpenAI API at host:port (another launcher, a tunnel …). It is only
        probed: no entry, no run, no adoption. A service that stops answering for --stall-after seconds unpowers gml8,
        which waits for it until ===L."""
        f = self.power_force
        p = core.probe(f["port"], self.args.probe_timeout, f["host"])
        here = self.llm.port == f["port"] and self.llm.host == f["host"]
        where = f"{f['host']}:{f['port']}"
        if p.get("state") == "up":
            f["fail_since"] = None
            if not here:
                self.power_id = None
                self.llm.target(f["port"], p.get("model") or "", "ext", host=f["host"])
                chat(f"⚡ powered by the external service {where} · {p.get('model') or '?'} (forced: no entry is touched)"
                     " — ask me anything", "DiagnosticOk")
                self.emit_state()
            return
        f["fail_since"] = f.get("fail_since") or time.time()
        if here and time.time() - f["fail_since"] >= self.args.stall_after:
            f["said"] = True
            self.llm.target(None)
            chat(f"⚡ the external service {where} stopped answering ({p.get('state')}): gml8 runs unpowered and waits "
                 "for it (===L alone: back to automatic)", "WarningMsg")
            self.emit_state()
        elif not here and self.llm.port:
            self.power_id = None
            self.llm.target(None)
            self.emit_state()
        if not here and not f.get("said"):
            f["said"] = True
            chat(f"⚡ waiting for the external service {where} ({p.get('state')}) · ===L alone: back to automatic", "WarningMsg")

    def power_tick(self):
        if self.power_force:
            return self.force_tick()
        cur = self.runs.runs.get(self.power_id) if self.power_id else None
        ups = self.ups()
        pick = None
        if self.power_pref:
            ref = self.power_pref
            pick = next((r for r in ups if str(r["port"]) == ref or r["key"] == ref or r["key"] == "#" + ref
                         or r["key"].lower() == ("@" + ref.lstrip("@")).lower()), None)
        if pick is None and cur and cur.get("state") == "up" and not self.power_pref:
            return
        if pick is None and ups and not self.power_pref:  # a preferred service (--llm, ===L) is waited for
            last = (core.read_json(os.path.join(self.mem.run_dir, "power.json"), {}) or {}).get("key")
            pick = next((r for r in ups if r["key"] == last), None) or min(ups, key=lambda r: r.get("ready_at") or "")
        if pick is not None:
            if cur is pick:
                return
            self.power_id = pick["id"]
            self.llm.target(pick["port"], pick.get("model_id") or "", pick["key"])
            core.atomic_write(os.path.join(self.mem.run_dir, "power.json"), json.dumps({"key": pick["key"], "at": utc_now()}))
            chat(f"⚡ powered by {pick['key']} :{pick['port']} · {os.path.basename(str(pick.get('model')))} — ask me anything",
                 "DiagnosticOk")
            self.power_waiting_said = False
            self.emit_state()
        elif cur is not None or self.llm.port:
            self.power_id = None
            self.llm.target(None)
            chat((f"⚡ waiting for {self.power_pref} (===L alone: any service)" if self.power_pref else
                  "⚡ no LLM service is up") + ": gml8 runs unpowered (launches, stops and probes still work)", "WarningMsg")
            self.emit_state()

    # ----- the inventory's marks, MODEL IDX, the bus
    def entry_run(self, e: Entry) -> Optional[dict]:
        return self.runs.of_key(e.key)

    def emit_inv(self):
        """The marks at INVENTORY's right edge, as chips: "parts" = [[text, role]] — role "edge" in the state's colours
        (Gml8Edge<state>), "port" the same colours inverted, "404" its own. PORT <port> only on the entry gml8
        launched, once it is up (an entry with the same port, a server started outside gml8: no mark)."""
        marks = {}
        for e in self.inv.load():
            m = {}
            if e.kind == "sep":
                m = {"line_hl": "Gml8Section"}
            elif e.kind == "bad":
                m = {"text": "✘ unreadable entry", "hl": "Gml8Bad"}
            elif e.kind in ("entry", "placeholder"):
                r = self.entry_run(e)
                own = bool(r and r.get("origin") == "gml8")
                if own and r.get("state") in ACTIVE:
                    st = r["state"]
                    m = {"sign": "⚡" if r["id"] == self.power_id else "▶", "sign_hl": STATE_HL.get(st, "Gml8Dim")}
                    if st == "up":
                        m.update(parts=[["PORT", "edge"], [str(r["port"]), "port"]], hl="Gml8Up")
                elif own and r.get("state") in ("crashed", "failed") and \
                        time.time() - (core.parse_utc(r.get("ended")) or 0) < 86400:
                    m = {"text": f"{ICON[r['state']]} {r['state']} {when(r.get('ended'))}", "hl": "Gml8Bad"}
                elif not core.file_facts(core.model_path(e.model))["exists"]:
                    m = {"parts": [["FILE", "edge"], ["404", "404"]], "hl": "Gml8Missing"}
                elif e.kind == "placeholder":
                    m = {"text": "◌ placeholder", "hl": "Gml8Placeholder"}
                if e.reserved and e.series and e.model != e.series and not m.get("parts"):
                    m["text"] = (m.get("text", "") + f"  ≠ series #{e.slot}").strip()
                    m.setdefault("hl", "Gml8Dim")
            elif e.kind == "blank" and e.reserved and e.series:
                m = {"text": f"◌ slot of {os.path.basename(e.series)}", "hl": "Gml8Placeholder"}
            if m:
                marks[str(e.line)] = m
        sig = json.dumps(marks, sort_keys=True)
        if sig != self.last_inv:
            self.last_inv = sig
            emit({"t": "inv", "marks": marks, "path": self.inv.path})

    def services(self) -> List[dict]:
        out = []
        for r in self.runs.active():
            e = self.inv.by_key(r["key"])
            out.append({"key": r["key"], "line": e.line if e else r.get("line"), "name": e.pname if e else "",
                        "profile": r.get("profile") or (e.profile if e else ""), "model": os.path.basename(str(r.get("model"))),
                        "model_file": r.get("model"), "model_id": r.get("model_id") or "", "port": r["port"],
                        "host": "127.0.0.1", "url": f"http://127.0.0.1:{r['port']}",
                        "base_url": f"http://127.0.0.1:{r['port']}/v1", "state": r["state"], "kind": r.get("kind", "chat"),
                        "vision": bool(r.get("vision")), "n_ctx": r.get("n_ctx"), "pid": r["pid"],
                        "started": r.get("started"), "ready_at": r.get("ready_at"), "origin": r.get("origin"),
                        "power": r["id"] == self.power_id, "tps": r.get("tps_gen"), "provider": "gml8",
                        "hostname": core.hostname()})
        return out

    def publish(self, force=False):
        svc = self.services()
        sig = json.dumps([{k: s[k] for k in ("key", "port", "state", "model_id", "power", "n_ctx", "vision")} for s in svc],
                         sort_keys=True)
        if sig != self.last_services or force:
            self.last_services = sig
            try:
                self.mem.write_services(svc)
            except OSError as e:
                logw(f"cannot write the services registry: {e}", "ErrorMsg")
            emit({"t": "services", "list": svc})

    def selected_entry(self) -> Optional[Entry]:
        if not self.sel:
            return None
        e = self.inv.get(self.sel["line"])
        text = self.sel.get("text")
        if text is not None and (e is None or text != e.raw):
            # the line as it is in the buffer (unsaved): parsed on its own, in the section the file has there
            items = core.parse_inventory(text, [])
            t = items[0] if items else Entry(self.sel["line"], text)
            t.line = self.sel["line"]
            if e is not None:
                t.section, t.slot, t.series = e.section, e.slot, e.series
            elif self.inv.reserved_end and t.line > self.inv.reserved_end:
                t.section = "NAMED"
            if t.reserved:
                t.key = f"#{t.line}"
            t.unsaved = True
            return t
        return e

    def emit_idx(self, stage=None, force=False):
        e = self.selected_entry()
        if e is None:
            return
        staged = None
        st_text = self.stages.get(e.key)
        if st_text is not None and e.launchable:
            staged = parse_stage(st_text)
        doc = self.render_idx(e, staged)
        r = self.entry_run(e) if e.launchable else None
        run_chip = None  # MODEL IDX's header: ⚡ :8090 (gml8's, up) · 🔀 :8080 (outside gml8) · ⏳ loading :8090 · 💥 crashed
        if r and r.get("state") in ACTIVE + ("crashed", "failed"):
            st = r["state"]
            run_chip = (("⚡" if r.get("origin") == "gml8" else "🔀") + f" :{r['port']}" if st == "up"
                        else f"{ICON.get(st, '•')} {st}" + (f" :{r['port']}" if st in ACTIVE else ""))
        msg = {"t": "idx", "key": e.key, "line": e.line, "lines": doc.lines, "hls": doc.hls,
               "title": f"{e.key} · {e.short or e.kind}", "run": run_chip,
               "log": (r or {}).get("log") if r and r.get("state") in ACTIVE + ("crashed", "failed") else None,
               "active": bool(r and r.get("state") in ACTIVE), "staged": bool(staged and stage_diff(e, staged)),
               "launchable": e.launchable}
        sig = json.dumps(msg, sort_keys=True)
        if stage is not None:
            msg["stage"] = stage
        elif sig == self.last_idx and not force:
            return
        self.last_idx = sig
        emit(msg)

    def render_idx(self, e: Entry, staged: Optional[dict] = None) -> Doc:
        d = Doc()
        unsaved = getattr(e, "unsaved", False)
        if e.kind == "sep":
            d.add((f" {e.model or 'SECTION'} ", "Gml8KeyChip"), ("  a section of the inventory", "Gml8Dim"))
            items = [x for x in self.inv.load() if x.section == e.section and x.launchable]
            d.add(f" {len(items)} entr{'y' if len(items) == 1 else 'ies'}: " + ", ".join(x.key for x in items[:12]))
            if e.section == "RESERVED" or e.line == self.inv.reserved_end + 1:
                d.add(("   lines above are RESERVED: line N = slot N of the series (open: gml8 adapts them)", "Gml8Dim"))
            return d
        if not e.launchable:
            d.add((f" line {e.line} ", "Gml8KeyChip"), (f"  {e.kind}", "Gml8Dim"), ("  (unsaved)" if unsaved else "", "Gml8Warn"))
            if e.reserved:
                if e.series:
                    d.add(" slot of series #", (str(e.slot), "Gml8Val"), ": ", (e.series, "Gml8Model"))
                    if e.kind == "blank":
                        d.add(("   empty: the merge at start puts '<model>: []' here", "Gml8Dim"))
                else:
                    d.add(("   a reserved line past the series", "Gml8Dim"))
            else:
                d.add((f"   section {e.section}", "Gml8Dim"))
            return d
        v = e.values()
        changes = []
        if staged:
            changes = stage_diff(e, staged)
            if changes:
                v.update(staged)
        cur = Entry(e.line, e.raw)
        cur.__dict__.update({k: getattr(e, k) for k in ("kind", "section", "slot", "series", "key", "comment")})
        cur.model, cur.profile, cur.params, cur.version, cur.device = v["model"], v["profile"], v["params"], v["version"], v["device"]
        cur.extra = v.get("extra") or {}
        r = self.entry_run(e)
        sp = core.build_spec(cur, None, avoid_ports={x["port"] for x in self.runs.active() if not r or x["id"] != r["id"]},
                             adapt=True)
        if r and r.get("state") in ACTIVE:
            sp.errors = [x for x in sp.errors if "is taken" not in x]
            sp.adapted = [x for x in sp.adapted if "--port" not in x]
        # header
        d.add((f" {e.key} ", "Gml8KeyChip"), (f" {cur.profile or 'CPU (no profile)'} ", "Gml8ProfileChip"),
              (f"  {'reserved · slot ' + str(e.slot) if e.reserved else 'named · ' + e.section}", "Gml8Dim"),
              ("  placeholder" if e.kind == "placeholder" else "", "Gml8Placeholder"),
              ("  unsaved line" if unsaved else "", "Gml8Warn"))
        if e.reserved and e.series:
            if e.series == cur.model:
                d.add(("  series #", "Gml8Dim"), (str(e.slot), "Gml8Val"), (" ✔ this model", "Gml8Ok"))
            else:
                d.add(("  series #", "Gml8Dim"), (str(e.slot), "Gml8Val"), (" is ", "Gml8Dim"),
                      (os.path.basename(e.series), "Gml8Val"), (" — this machine runs another file here", "Gml8Dim"))
        if r:
            st = r.get("state")
            parts = [(f" {ICON.get(st, '•')} {st} ", STATE_HL.get(st, "Gml8Dim") + "Chip"), ("  ", None)]
            if st in ACTIVE:
                parts += [(f":{r['port']}", "Gml8Port"), (f" · pid {r['pid']} · {core.ago(r.get('t0'))}", "Gml8Dim")]
                if r.get("tps_gen"):
                    parts.append((f" · {r['tps_gen']:.1f} t/s", "Gml8Val"))
                if r["id"] == self.power_id:
                    parts.append((" · ⚡ powers gml8", "Gml8Ok"))
                if r.get("origin") == "foreign":
                    parts.append((" · 🔀 started outside gml8", "Gml8Warn"))
            else:
                parts.append((f"{when(r.get('ended') or r.get('started'))}", "Gml8Dim"))
            d.add(*parts)
        else:
            d.add((" ○ not running ", "Gml8DimChip"), ("  L launches it", "Gml8Dim"))
        if changes:
            d.add(("✎ STAGED, not in the inventory: ", "Gml8Staged"), ("; ".join(changes), "Gml8Staged"))
            d.add(("  ss shows it · L launches the stage · R relaunches with it · C commits it · u drops it", "Gml8Dim"))
        # model
        d.head("MODEL")
        ff = core.file_facts(sp.model_abs)
        d.kv("file", (cur.model, "Gml8Model"))
        if ff["exists"]:
            d.kv("", ("✔ ", "Gml8Ok"), (gib(ff["size"]), "Gml8Val"),
                 (f" · {when(ff['mtime'])} · {tilde(os.path.dirname(sp.model_abs))}"
                  + (f" · {ff['parts']} parts" if ff["parts"] > 1 else ""), "Gml8Dim"))
        else:
            d.kv("", ("✘ not on this machine: ", "Gml8Bad"), (tilde(sp.model_abs), "Gml8Dim"))
        f = self.mem.gguf.facts(sp.model_abs) if ff["have"] else None
        if f and not f.get("error"):
            d.kv("gguf", (f["arch"], "Gml8Val"), " · ", (f["name"] or "?", "Gml8Val"),
                 (f" · {f['size_label']}" if f.get("size_label") else ""), " · ", (f["ftype"], "Gml8Val"),
                 (f" · MoE {f['experts_used']}/{f['experts']}" if f.get("experts") else ""))
            hkv = f["n_head_kv"]
            hkv_s = (f"{min(hkv)}–{max(hkv)}" if isinstance(hkv, list) and hkv and min(hkv) != max(hkv)
                     else str(hkv[0] if isinstance(hkv, list) and hkv else hkv))
            d.kv("shape", (str(f["n_layer"]), "Gml8Val"), " layers · embd ", (str(f["n_embd"]), "Gml8Val"),
                 " · heads ", (f"{f['n_head']}/{hkv_s} kv", "Gml8Val"), " · head ", (str(f["head_k"]), "Gml8Val"),
                 (f" (swa {f['head_k_swa']})" if f.get("head_k_swa") else ""))
            ctxp = [(" train ", None), (str(f["ctx_train"]), "Gml8Val")]
            if f.get("swa"):
                n_swa = sum(1 for x in (f.get("swa_pattern") or []) if x) or None
                ctxp += [" · SWA ", (str(f["swa"]), "Gml8Val"), (f" on {n_swa} layers" if n_swa else "")]
            if f.get("shared_kv"):
                ctxp += [" · KV shared by ", (str(f["shared_kv"]), "Gml8Val"), " layers"]
            d.kv("context", *ctxp)
            d.kv("vocab", (str(f["vocab"]), "Gml8Val"), " · chat template ", ("✔", "Gml8Ok") if f["template"] else ("✘", "Gml8Bad"))
        elif f and f.get("error"):
            d.kv("gguf", ("✘ " + f["error"], "Gml8Bad"))
        mm = core.mmproj_near(sp.model_abs)
        pairs = core.parse_params(core.expand_params(cur.params))
        if mm:
            loaded = core.pget(pairs, "mmproj")
            d.kv("vision", (os.path.basename(mm[0][0]), "Gml8Val"), (f" ({gib(mm[0][1])})", "Gml8Dim"),
                 ("  loaded (-mm) ✔" if loaded else "  next to it, not loaded (add -mm to see images)",
                  "Gml8Ok" if loaded else "Gml8Dim"))
        # launch
        d.head("LAUNCH")
        dv = self.hw.devices(sp.bin) if os.path.exists(sp.bin) else {"devices": [], "version": "", "err": "missing"}
        if sp.script:
            d.kv("script", (tilde(sp.script), "Gml8Val"), ("  (instead of llama-server; GML8_* in its environment)", "Gml8Dim"))
        d.kv("build", (cur.accel, "Gml8Val"), " → ", (tilde(sp.bin), "Gml8Val" if os.path.exists(sp.bin) else "Gml8Bad"),
             (f" · {dv.get('version')}" if dv.get("version") else ""),
             (" · " + ", ".join(f"{x['name']} {x['desc']}" for x in dv["devices"]) if dv["devices"] else " · CPU only", "Gml8Dim"))
        changed = set()
        if changes:
            a, b = core.parse_params(e.params), core.parse_params(cur.params)
            for fl, _ in a + b:
                n = core.ALIAS.get(fl, fl) if fl else None
                if n and core.pget(a, n) != core.pget(b, n):
                    changed.add(n)
        shown = set()
        for label, names in core.GROUPS:
            parts = param_parts(pairs, names, changed)
            if parts:
                d.kv(label, *parts)
                shown.update(names)
        other = [[fl, val] for fl, val in pairs if fl and core.ALIAS.get(fl, fl) not in shown or not fl]
        if other:
            d.kv("other", *param_parts(other, {core.ALIAS.get(fl, fl) if fl else None for fl, _ in other}, changed))
        if not pairs:
            d.kv("params", ("none — llama-server's defaults", "Gml8Dim"))
        if cur.device:
            d.kv("device", (core.envsubst(cur.device) or f"{cur.device} (empty: not set)", "Gml8Val"))
        if cur.extra and not e.reserved:
            ex = cur.extra
            if ex.get("env"):
                d.kv("env", ", ".join(f"{k}={val}" for k, val in ex["env"].items()))
            for k in ("pre", "post", "port", "wait"):
                if ex.get(k):
                    d.kv(k, str(ex[k]))
        for x in sp.errors:
            d.add(("  ✘ " + x, "Gml8Bad"))
        for x in sp.warnings:
            d.add(("  ⚠ " + x, "Gml8Warn"))
        for x in sp.adapted:
            d.add(("  ↺ adapts " + x, "Gml8Staged"))
        # capacity
        d.head("CAPACITY")
        if ff["have"]:
            ram = self.hw.memory()
            est = core.estimate(f if f and not f.get("error") else None, ff["size"], core.parse_params(sp.params), cur.accel,
                                dv["devices"], ram, mm[0][1] if mm else 0)
            d.kv("needs", "weights ", (gib(est["weights"]), "Gml8Val"), " + KV ", (gib(est["kv"]), "Gml8Val"),
                 (f" ({est['n_ctx']} ctx)", "Gml8Dim"), " + compute ", (gib(est["compute"]), "Gml8Val"),
                 (" + mmproj " if est["mmproj"] else ""), (gib(est["mmproj"]) if est["mmproj"] else "", "Gml8Val"))
            if est["ctx_note"]:
                d.kv("", (est["ctx_note"], "Gml8Warn"))
            dev = est["device"]
            if dev and est["frac"] > 0:
                ok = est["gpu_need"] <= dev["free"]
                d.kv("GPU", (dev["name"], "Gml8Val"), f" {gib(est['gpu_need'])} of ", (gib(dev["free"]), "Gml8Val"),
                     f" free ({gib(dev['total'])}) ", MARK["ok" if ok else "warn"])
            d.kv("RAM", f"{gib(est['ram_need'])} of ", (gib(ram.get("avail")), "Gml8Val"),
                 f" available ({gib(ram.get('total'))}) ",
                 MARK["ok" if not ram.get("avail") or est["ram_need"] <= ram["avail"] else "warn"])
            vd = est["verdict"]
            d.kv("verdict", {"ok": ("✔ fits", "Gml8Ok"), "tight": ("⚠ tight", "Gml8Warn"),
                             "over": ("⚠ over: it runs slower or fails", "Gml8Warn"),
                             "no": ("✘ does not fit", "Gml8Bad")}.get(vd, (vd, None)),
                 (" (measured now: running services hold their share)" if self.runs.active() else "", "Gml8Dim"))
            for lvl, text in est["notes"]:
                d.add((f"  {MARK[lvl][0]} {text}", MARK[lvl][1]))
        else:
            d.kv("", ("the model file is missing: nothing to measure", "Gml8Dim"))
        # run
        if r:
            d.head("RUN")
            st = r.get("state")
            if st in ACTIVE or r.get("ready_at"):
                d.kv("since", when(r.get("started")),
                     (f" · loaded in {r['load_secs']} s" if r.get("load_secs") else ""),
                     (f" · n_ctx {r['n_ctx']}" if r.get("n_ctx") else ""), (" · vision" if r.get("vision") else ""))
            if r.get("model_id"):
                d.kv("serves", (r["model_id"], "Gml8Val"), (f"  {r.get('build')}" if r.get("build") else "", "Gml8Dim"))
            if r.get("tps_gen") or r.get("tps_prompt"):
                d.kv("speed", "prompt ", (f"{r.get('tps_prompt') or 0:.1f}", "Gml8Val"), " t/s · gen ",
                     (f"{r.get('tps_gen') or 0:.1f}", "Gml8Val"), " t/s (last request)")
            if r.get("staged"):
                d.kv("", ("launched with staged params (not committed)", "Gml8Staged"))
            for x in r.get("adapted") or []:
                d.kv("", ("↺ adapted " + x, "Gml8Staged"))
            for dg in r.get("diag") or []:
                d.add((f"  ✘ {dg['cause']}", "Gml8Bad"))
                d.add((f"    {dg['evidence']}", "Gml8Dim"))
                d.add((f"    fix: {dg['fix']}", "Gml8Warn"))
            if r.get("log"):
                d.kv("log", (tilde(r["log"]), "Gml8Dim"))
            d.add(("  $ " + clip(core.mask(r.get("shell") or ""), 400), "Gml8Cmd"))
        else:
            d.add(("  $ " + core.mask(sp.shell()), "Gml8Cmd"))
        # history
        mem = self.mem.entry(e.key)
        evs = self.mem.events(e.key, 6, kinds=("launch", "up", "crash", "fail", "stop", "commit", "heal", "note", "found"))
        if evs or mem:
            d.head("HISTORY")
            if mem:
                ls = [x for x in (mem.get("load_secs") or []) if x]
                d.kv("record", f"{mem.get('launches', 0)} launches · {mem.get('ups', 0)} up · {mem.get('fails', 0)} failed"
                     + (f" · loads in ~{int(sum(ls) / len(ls))} s" if ls else ""))
                for n in (mem.get("notes") or [])[-3:]:
                    d.kv("note", (n["text"], "Gml8Val"), (f"  {when(n['at'])}", "Gml8Dim"))
            for ev in reversed(evs):
                txt = ev["ev"] + (f" :{ev['port']}" if ev.get("port") else "") + \
                      (f" · {ev['secs']} s" if ev.get("secs") else "") + (f" · {ev['cause']}" if ev.get("cause") else "") + \
                      (f" · {ev.get('result')}" if ev.get("result") else "") + (f" · {clip(ev.get('text'), 60)}" if ev.get("text") else "")
                d.add((f"  {when(ev['at'])} ", "Gml8Dim"), txt)
        return d

    def run_line(self, r: dict) -> str:
        return (f"{r['key']} :{r['port']} {r.get('state')} · {os.path.basename(str(r.get('model')))}"
                + (f" · n_ctx {r['n_ctx']}" if r.get("n_ctx") else "") + (f" · {r['tps_gen']:.1f} t/s" if r.get("tps_gen") else "")
                + (" · vision" if r.get("vision") else "") + (f" · {r.get('kind')}" if r.get("kind") not in (None, "chat") else "")
                + (" · foreign" if r.get("origin") == "foreign" else "") + (" · powers gml8" if r["id"] == self.power_id else "")
                + (f" · since {when(r.get('started'))}" if r.get("state") in ACTIVE else f" · {when(r.get('ended'))}"))

    # ----- operations (keys, commands, the LLM)
    def ref_entry(self, ref) -> Optional[Entry]:
        return self.inv.find(ref) if ref not in (None, "") else None

    def launch_entry(self, e: Entry, values: Optional[dict] = None, origin: str = "user", wait: bool = False) -> str:
        with self.launch_lock:
            cur = self.runs.of_key(e.key)
            if cur and cur.get("state") in ACTIVE:
                return f"{e.key} already runs on :{cur['port']} ({cur['state']}) — relaunch to change it"
            staged = bool(values)
            sp = core.build_spec(e, values, avoid_ports={x["port"] for x in self.runs.active()}, adapt=True)
            if sp.errors:
                return "✘ " + " · ".join(sp.errors)
            try:
                r = self.runs.start(sp, staged=staged, origin=origin)
            except Exception as ex:
                return f"✘ launch failed: {type(ex).__name__}: {ex}"
            msg = f"🚀 {e.key} launching on :{sp.port} (pid {r['pid']})" + (" with the staged params" if staged else "")
            for x in sp.adapted:
                msg += f"\n   ↺ adapted {x}"
            if sp.adapted and e.reserved and not staged:
                line = core.adapted_line(e, sp)
                if line:
                    try:
                        self.inv.write_line(e.line, line, expect=e.raw, mem=self.mem, why="adapted at launch")
                        msg += f"\n   ✎ written to line {e.line} (a reserved entry is open to gml8)"
                        self.stages.pop(e.key, None)
                        emit({"t": "req", "id": 0, "op": "reload", "path": self.inv.path})
                    except ValueError as ex:
                        msg += f"\n   (not written: {ex})"
            self.hw.forget()
        self.wake.set()
        if not wait:
            return msg
        chat(msg, "Comment")
        return self.wait_run(r)

    def wait_run(self, r: dict, limit: float = 240) -> str:
        end = time.time() + min(limit, float(r.get("wait") or 300))
        while time.time() < end:
            if self.cancel.is_set():
                raise Cancelled()
            st = r.get("state")
            if st == "up":
                return f"{r['key']} is up on :{r['port']} after {r.get('load_secs')} s (model {r.get('model_id')}, n_ctx {r.get('n_ctx')})"
            if st in ("crashed", "failed", "stopped"):
                d = r.get("diag") or []
                return (f"{r['key']} {st}" + (f": {d[0]['cause']} — evidence: {d[0]['evidence']} — fix: {d[0]['fix']}" if d else "")
                        + "\nlog tail:\n" + "\n".join(core.log_tail(r.get("log"), 15)))
            time.sleep(1)
        return f"{r['key']} is still {r.get('state')} on :{r['port']} after {int(time.time() - r['t0'])} s"

    def stop_ref(self, ref=None, port=None, why="user") -> str:
        r = None
        if port:
            r = self.runs.of_port(int(port))
        if r is None and ref not in (None, ""):
            e = self.ref_entry(ref)
            if e:
                r = self.runs.of_key(e.key)
            elif str(ref).isdigit():
                r = self.runs.of_port(int(ref))
        if not r or r.get("state") not in ACTIVE:
            return f"nothing runs for {ref or port}"
        res = self.runs.stop(r, why)
        if r["id"] == self.power_id:
            self.power_id = None
            self.llm.target(None)
        self.hw.forget()
        self.wake.set()
        return f"⏹ {r['key']} :{r['port']} {res}"

    def relaunch(self, e: Entry, values: Optional[dict], origin="user", wait=False) -> str:
        r = self.runs.of_key(e.key)
        out = []
        if r and r.get("state") in ACTIVE:
            out.append(self.stop_ref(e.key, why=f"relaunch by {origin}"))
            core.wait_port_free(r.get("port"), 2.0)  # its old port back before the launch, not a new one adapted
        out.append(self.launch_entry(e, values, origin, wait=wait))
        return "\n".join(out)

    def commit(self, e: Entry, values: dict, why="user") -> str:
        if not values:
            return "nothing staged"
        params = values.get("params") or ""
        if norm_params(params) == norm_params(e.params):
            params = e.params.rstrip()  # unchanged params keep their spelling
        line = core.format_entry(values.get("model") or e.model, values.get("profile") or "", params,
                                 values.get("version") or "", values.get("device") or "",
                                 None if e.reserved else (values.get("extra") or None), e.comment)
        if not stage_diff(e, values):
            return "the stage is the entry already"
        try:
            res = self.inv.write_line(e.line, line, expect=e.raw, mem=self.mem, why=why)
        except ValueError as ex:
            return f"✘ {ex}"
        self.stages.pop(e.key, None)
        emit({"t": "req", "id": 0, "op": "reload", "path": self.inv.path})
        return f"✎ line {e.line} {res}: {clip(core.mask(line), 200)}"

    def staged_values(self, e: Entry, stage_text: Optional[str]) -> Optional[dict]:
        text = stage_text if stage_text is not None else self.stages.get(e.key)
        if text is None:
            return None
        v = parse_stage(text)
        return v if stage_diff(e, v) else None

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
            except Exception as ex:
                chat(f"❌ {msg.get('op')}: {type(ex).__name__}: {ex}", "ErrorMsg")

    def op_entry(self, msg) -> Optional[Entry]:
        if msg.get("ref"):
            return self.ref_entry(msg["ref"])
        if msg.get("key"):
            e = self.inv.by_key(msg["key"])
            if e:
                return e
        text = msg.get("text")
        if msg.get("line") and text is not None:
            e = self.inv.get(int(msg["line"]))
            if e and e.raw != text:  # the file moved under the buffer (a merge, an edit): find the line by its text
                e = next((x for x in self.inv.load() if x.raw == text and x.launchable), None)
                if e is None and text.strip():
                    chat(f"✘ line {msg['line']} has unsaved edits: :w first, or stage them (L launches the STAGE)", "ErrorMsg")
                    return None
            if e and e.launchable:
                return e
        if msg.get("line"):
            e = self.inv.get(int(msg["line"]))
            if e and e.launchable:
                return e
        return None

    def run_op(self, msg):
        op = msg.get("op")
        if op == "select":
            line = int(msg.get("line") or 0)
            e = self.inv.get(line)
            prev = (self.sel or {}).get("key")
            self.sel = {"line": line, "text": msg.get("text"), "modified": bool(msg.get("modified"))}
            cur = self.selected_entry()
            if cur is None:
                return
            self.sel["key"] = cur.key
            stage = None
            if cur.key != prev or msg.get("fresh"):
                stage = (self.stages.get(cur.key) or "\n".join(stage_lines(cur))).split("\n") if cur.launchable else []
            self.emit_idx(stage=stage, force=True)
            self.emit_state()
            return
        if op == "stage":
            key = msg.get("key")
            if key:
                self.stages[key] = msg.get("text") or ""
            self.emit_idx(force=True)
            return
        if op == "unstage":
            e = self.op_entry(msg) or self.selected_entry()
            if e:
                self.stages.pop(e.key, None)
                self.emit_idx(stage=stage_lines(e) if e.launchable else [], force=True)
            return
        if op in ("launch", "relaunch", "commit"):
            e = self.op_entry(msg)
            if e is None:
                return chat(f"✘ {msg.get('ref') or msg.get('key') or 'line ' + str(msg.get('line'))} is not an entry", "ErrorMsg")
            if msg.get("stage") is not None:
                self.stages[e.key] = msg["stage"]
            values = self.staged_values(e, msg.get("stage"))
            if op == "commit":
                if msg.get("modified"):
                    return chat("✘ the inventory has unsaved changes: :w first, then commit", "ErrorMsg")
                res = self.commit(e, values)
                chat(res, "ErrorMsg" if res.startswith("✘") else "Comment")
                self.emit_idx(stage=stage_lines(self.inv.by_key(e.key) or e), force=True)
                return
            chat("")
            chat(f"▶ {op} {e.key} {e.short}" + (" (staged)" if values else ""), "Title")
            res = self.relaunch(e, values, msg.get("origin") or "user") if op == "relaunch" else \
                self.launch_entry(e, values, msg.get("origin") or "user")
            chat(res, "ErrorMsg" if res.startswith("✘") else "Comment")
            self.emit_idx(force=True)
            return
        if op == "stop":
            e = self.op_entry(msg) if (msg.get("line") or msg.get("ref") or msg.get("key")) else None
            res = self.stop_ref(e.key if e else msg.get("ref"), msg.get("port"))
            chat(res, "Comment")
            return
        if op == "merge":
            res = core.merge_series(self.inv.path, self.inv.series_path, self.mem)
            chat("🧬 " + core.merge_text(res), "Comment")
            if res["changed"]:
                emit({"t": "req", "id": 0, "op": "reload", "path": self.inv.path})
            self.inv.load(force=True)
            self.wake.set()
            return
        if op == "probe":
            self.wake.set()
            return
        if op == "hw":
            self.hw.forget()
            chat(self.hardware_text(), "Comment")
            self.emit_idx(force=True)
            return
        if op == "provide":
            self.provide(msg.get("rid"), msg.get("want") or {})
            return

    # ----- the bus: an agent asks for a service
    def provide(self, rid, want: dict):
        svc = core.rank(self.services(), want.get("kind", "chat"), want.get("vision"), int(want.get("min_ctx") or 0),
                        want.get("name"), want.get("model"), want.get("port"))
        if svc:
            return emit({"t": "provided", "rid": rid, "service": svc[0]})
        busy = [s for s in self.services() if s["state"] in ("launching", "loading") and
                (not want.get("name") or want["name"].lower() in f"{s['key']} {s['name']}".lower())]
        if busy:
            self.provides.append({"rid": rid, "want": want, "until": time.time() + 600})
            return emit({"t": "provided", "rid": rid, "service": None, "launching": busy[0]})
        e = self.pick_entry(want)
        if not e:
            return emit({"t": "provided", "rid": rid, "service": None, "error": "no entry of the inventory matches " + json.dumps(want)})
        values = None
        if want.get("vision") and not core.pget(core.parse_params(e.params), "mmproj") and e.reserved:
            mm = core.mmproj_near(core.model_path(e.model))
            if mm:
                values = e.values()
                values["params"] = core.join_params(core.pset(core.parse_params(e.params), "-mm", mm[0][0]))
        res = self.launch_entry(e, values, origin=f"bus:{want.get('by') or 'agent'}")
        chat(f"🛎 {want.get('by') or 'an agent'} asked for a service {json.dumps(want)} → {res}", "Comment")
        if res.startswith("✘"):
            return emit({"t": "provided", "rid": rid, "service": None, "error": res})
        self.provides.append({"rid": rid, "want": want, "until": time.time() + 600})
        r = self.runs.of_key(e.key)
        emit({"t": "provided", "rid": rid, "service": None, "launching": {"key": e.key, "port": r["port"] if r else None}})

    def provide_tick(self):
        keep = []
        for p in self.provides:
            w = p["want"]
            svc = core.rank(self.services(), w.get("kind", "chat"), w.get("vision"), int(w.get("min_ctx") or 0),
                            w.get("name"), w.get("model"))
            if svc:
                emit({"t": "provided", "rid": p["rid"], "service": svc[0]})
            elif time.time() > p["until"]:
                emit({"t": "provided", "rid": p["rid"], "service": None, "error": "no service came up in time"})
            else:
                keep.append(p)
        self.provides = keep

    def pick_entry(self, want: dict) -> Optional[Entry]:
        cands = [e for e in self.inv.entries() if core.file_facts(core.model_path(e.model))["exists"]]
        if want.get("name"):
            n = want["name"].lower()
            cands = [e for e in cands if n in e.key.lower() or n in e.pname.lower() or n in e.model.lower()]
        if want.get("model"):
            cands = [e for e in cands if want["model"].lower() in e.model.lower()]
        kind = want.get("kind", "chat")
        cands = [e for e in cands if (("embed" if core.pget(core.parse_params(e.params), "embed") else "chat") == kind)]
        if want.get("vision"):
            vis = [e for e in cands if core.pget(core.parse_params(e.params), "mmproj")]
            cands = vis or [e for e in cands if e.reserved and core.mmproj_near(core.model_path(e.model))]
        if not cands:
            return None
        last = {}
        for ev in self.mem.events(None, 200, kinds=("up",)):
            last[ev.get("key")] = ev["at"]
        return sorted(cands, key=lambda e: (e.kind == "placeholder", last.get(e.key) is None, -len(last.get(e.key) or ""),
                                            e.line))[0]

    # ----- text views
    def hardware_text(self) -> str:
        ram = self.hw.memory(0)
        out = [f"🖥  {core.hostname()} · {ram.get('cpu')} · {ram.get('cores')} threads · RAM {gib(ram.get('avail'))} "
               f"available of {gib(ram.get('total'))}"]
        for acc in core.hw_builds():
            b = core.server_bin(acc)
            dv = self.hw.devices(b, 0)
            out.append(f"   {acc:4s} build {tilde(b)} {dv.get('version')}: " + (
                ", ".join(f"{d['name']} {d['desc']} {gib(d['free'])} free of {gib(d['total'])}" for d in dv["devices"])
                or dv.get("err") or "CPU only"))
        return "\n".join(out)

    def status_text(self) -> str:
        act = self.runs.active()
        out = [f"services ({len(act)}):"] + ["  " + self.run_line(r) for r in act]
        done = [r for r in self.runs.runs.values() if r.get("state") in ("crashed", "failed")
                and time.time() - (core.parse_utc(r.get("ended")) or 0) < 86400]
        if done:
            out.append("recent failures:")
            out += ["  " + self.run_line(r) + (f" · {r['diag'][0]['cause']}" if r.get("diag") else "") for r in done[-5:]]
        p = self.power_info()
        out.append((f"gml8 is powered by the external service {p['host']}:{p['port']} (===Lf: not an entry, not tracked)"
                    if p.get("external") else f"gml8 is powered by {p['key']} :{p['port']}") if p
                   else "gml8 is unpowered (no chat service up)")
        return "\n".join(out)

    def entries_text(self, query: str = "", limit: int = 40) -> str:
        out, ph = [], 0
        q = (query or "").lower()
        for e in self.inv.load():
            if e.kind == "sep":
                out.append(f"---- {e.model} ----")
                continue
            if not e.launchable:
                continue
            if q and q not in f"{e.key} {e.model} {e.profile} {e.params}".lower():
                continue
            if e.kind == "placeholder" and not q:
                ph += 1
                continue
            r = self.entry_run(e)
            state = f" [{r['state']} :{r['port']}]" if r and r.get("state") in ACTIVE else ""
            have = "" if core.file_facts(core.model_path(e.model))["exists"] else " (model file missing)"
            out.append(f"{e.key} {e.model} {e.profile!r} params: {clip(core.mask(e.params), 160)}{state}{have}")
        if ph:
            out.append(f"(+{ph} placeholders: reserved slots of the series without params)")
        return "\n".join(out[:limit]) or "(no entries match)"

    def entry_text(self, e: Entry) -> str:
        doc = self.render_idx(e, self.staged_values(e, None))
        return "\n".join(doc.lines)

    def models_text(self, query: str = "") -> str:
        out = []
        base = core.gmlbase()
        for root, _, files in os.walk(base):
            for n in files:
                if n.endswith(".gguf") and (not query or query.lower() in os.path.join(root, n).lower()):
                    p = os.path.join(root, n)
                    try:
                        out.append(f"{os.path.relpath(p, base)} ({gib(os.path.getsize(p))})")
                    except OSError:
                        pass
        return "\n".join(sorted(out)[:60]) or "(no model files match)"

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
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if text in ("===h", "?", ":help"):
            return chat(HELP_TEXT, "Comment")
        if text == "===a":
            self.auto = not self.auto
            chat(f"auto-approve (launch, stop, commit, shell, editor from the LLM): {'ON' if self.auto else 'off'}", "WarningMsg")
            return self.emit_state()
        if text == "===x":
            return self.do_cancel()
        if text == "====":
            self.recent.clear()
            return chat("context reset: earlier requests forgotten", "Comment")
        if text == "===T":
            self.llm.think = not self.llm.think
            chat(f"LLM thinking: {'on' if self.llm.think else 'OFF'}", "WarningMsg")
            return self.emit_state()
        if text == "===r":
            self.wake.set()
            return chat("probing…", "Comment")
        if text == "===l":
            return chat(self.status_text(), "Comment")
        if text == "===m":
            return self.on_op({"op": "merge"})
        if text == "===hw":
            return self.on_op({"op": "hw"})
        if cmd in ("===w", "===R"):
            e = self.ref_entry(arg) or self.selected_entry()
            if not e or not e.launchable:
                return chat(f"✘ no entry {arg}", "ErrorMsg")
            return self.on_op({"op": "launch" if cmd == "===w" else "relaunch", "key": e.key})
        if cmd == "===k":
            port = int(arg) if arg.isdigit() and not self.ref_entry(arg) else None
            return self.on_op({"op": "stop", "key": None if port else arg, "port": port})
        if cmd == "===L":
            was_forced, self.power_force = self.power_force, None
            self.power_pref = arg
            self.power_id = None if (arg or was_forced) else self.power_id
            chat(f"⚡ power: {arg or 'automatic (the first service up)'}", "Comment")
            if arg.isdigit() and not any(str(r.get("port")) == arg for r in self.runs.active()) \
                    and core.probe(int(arg), 1.5).get("state") == "up":
                chat(f"  :{arg} answers, but no service gml8 runs or found is there (another launcher, a tunnel …): "
                     f"===Lf {arg} powers gml8 from it as it is", "WarningMsg")
            self.wake.set()
            return
        if cmd == "===Lf":
            m = re.match(r"^(?:https?://)?(?:([\w.\-]+):)?(\d{2,5})(?:/.*)?$", arg)
            if not m:
                return chat("✘ ===Lf <port> (or host:port): power gml8 from the OpenAI API there, as it is", "ErrorMsg")
            self.power_force = {"host": m.group(1) or "127.0.0.1", "port": int(m.group(2))}
            self.power_id = None
            chat(f"⚡ power: forced to {self.power_force['host']}:{self.power_force['port']} as it is (no entry or run is "
                 "touched · ===L alone: back to automatic)", "Comment")
            self.wake.set()
            return
        if cmd == "===s":
            hits = [ev for ev in self.mem.events(None, 400) if arg.lower() in json.dumps(ev, ensure_ascii=False).lower()]
            return chat("\n".join("  " + when(ev["at"]) + " " + clip(json.dumps({k: v for k, v in ev.items() if k != "at"},
                                                                                ensure_ascii=False), 200) for ev in hits[-15:])
                        or "  (no event matches)", "Comment")
        if text.startswith("===n"):
            return self.net_command(text[4:])
        chat("")
        chat("▶ " + text, "Title")
        if self.busy:
            chat("  (queued after the current task)", "Comment")
        self.submit(text, None, ctx)

    def do_cancel(self):
        if self.busy or self.pending:
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

    # ----- editor requests and approvals
    def nvim(self, op, timeout=None, **params) -> dict:
        with self.rlock:
            self.req_id += 1
            rid = self.req_id
            ev = threading.Event()
            self.reqs[rid] = {"ev": ev, "res": None}
        emit(dict({"t": "req", "id": rid, "op": op}, **params))
        end = time.time() + (timeout or 30)
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
        chat(f"  ❓ allow {what} ?  [y]es / [n]o / [a]lways", "WarningMsg")
        emit({"t": "show"})
        self.set_phase("approval")
        while not ev.wait(0.2):
            if self.cancel.is_set():
                break
        ans = self.approval["answer"]
        self.approval = None
        if ans.startswith("a"):
            self.auto = True
        ok = ans[:1] in ("y", "a")
        chat("    approved" if ok else "    denied", "Comment")
        self.set_phase("working")
        return ok

    # ----- the LLM loop
    def wait_power(self, limit: float = 900):
        end = time.time() + limit
        said = False
        while not self.llm.ready():
            if self.cancel.is_set():
                raise Cancelled()
            if not said:
                chat("  ⚡ no LLM service is up to power me: launch one (gml8w on a line, L in MODEL IDX, ===w <ref>); "
                     "your request waits", "WarningMsg")
                said = True
                self.set_phase("waiting for power")
            if time.time() > end:
                raise RuntimeError("no LLM service came up")
            time.sleep(0.5)

    def worker(self):
        while self.running:
            try:
                text, origin, ctx = self.tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            self.pending = max(0, self.pending - 1)
            self.current = (text, origin)
            self.cancel.clear()
            self.set_phase("working")
            if origin:
                chat("")
                chat(f"🌐 network request #{origin.get('id')} from @{origin.get('from')}: {clip(text, 300)}", "Special")
            logw(f"▶ {clip(text, 200)}", "Title")
            status, reply = "FAILED", ""
            try:
                self.wait_power()
                status, reply = self._run_task(text, origin, ctx)
            except Cancelled:
                status, reply = "CANCELLED", "Cancelled."
            except Exception as e:
                status, reply = "FAILED", f"{type(e).__name__}: {e}"
            chat(f"{'✔' if status == 'SUCCESS' else '✖'} {status}", "DiagnosticOk" if status == "SUCCESS" else "ErrorMsg")
            if reply:
                chat(reply)
            if status == "SUCCESS" and not origin and not (ctx or {}).get("auto"):
                self.recent.append(f"- {clip(text, 160)} → {clip(reply, 200)}")
            if origin:
                self.iface.answer(origin, status, reply)
            self.current = None
            self.set_phase("idle")
            if self.iface.connected:
                self.iface.set_state(self.iface.idle_state())

    def context_block(self, text, origin, ctx) -> str:
        ram = self.hw.memory()
        gpu = self.hw.devices(core.server_bin("GPU"))
        parts = [f"[TODAY] {utc_now()} (UTC)",
                 f"[THIS MACHINE] {core.hostname()} · {ram.get('cpu')} · RAM {gib(ram.get('avail'))} available of "
                 f"{gib(ram.get('total'))} · GPU build: " + (", ".join(f"{d['name']} {d['desc']} {gib(d['free'])} free of "
                                                                    f"{gib(d['total'])}" for d in gpu["devices"]) or "no GPU"),
                 f"[INVENTORY] {tilde(self.inv.path)} · {self.inv.reserved_end} reserved lines (series of "
                 f"{len([s for s in self.inv.series if s])})\n" + self.entries_text(limit=30),
                 "[SERVICES]\n" + self.status_text()]
        evs = self.mem.events(None, 6)
        if evs:
            parts.append("[RECENT EVENTS]\n" + "\n".join(f"{when(ev['at'])} {ev['ev']} {ev.get('key', '')} "
                                                         f"{':' + str(ev['port']) if ev.get('port') else ''} "
                                                         f"{clip(ev.get('cause') or ev.get('why') or '', 80)}" for ev in evs))
        e = self.selected_entry()
        if e is not None and e.launchable and not origin:
            parts.append(f"[SELECTED] the entry in MODEL IDX (\"this\", \"it\"): {e.key} {e.model} {e.profile!r} "
                         f"params: {clip(core.mask(e.params), 200)}")
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network.")
        elif self.recent:
            parts.append("[EARLIER SUCCESSFUL REQUESTS]\n" + "\n".join(self.recent))
        parts.append(f"[NETWORK] {self.iface.net.network} as @{self.iface.net.name}; members: {self.iface.net.who()}"
                     if self.iface.connected else "[NETWORK] not joined")
        return "\n\n".join(parts)

    def _run_task(self, text, origin, ctx):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self.context_block(text, origin, ctx) + f"\n\n[REQUEST]\n{text}"}]
        seen = {}
        for turn in range(1, self.args.max_turns + 1):
            if turn == self.args.max_turns - 1:
                messages.append({"role": "user", "content": "System: two turns left: declare_result now with what you know."})
            if self.cancel.is_set():
                raise Cancelled()
            self.wait_power()
            self.set_phase(f"thinking {turn}/{self.args.max_turns}")
            logw(f"── turn {turn}/{self.args.max_turns} · {self.llm.label} :{self.llm.port} ──", "Comment")
            self.trim(messages)
            try:
                raw = self.llm.stream(messages, self.cancel)
            except Exception as e:
                if self.cancel.is_set():
                    raise Cancelled()
                self.llm.model = None
                chat(f"  (LLM error: {clip(str(e), 160)}; trying again)", "Comment")
                time.sleep(2)
                continue
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
            sig = json.dumps({k: v for k, v in act.items() if k in ("action_type", "ref", "entry", "port", "query", "command",
                                                                    "set", "unset", "lines")}, sort_keys=True)
            if kind != "declare_result" and sig in seen:
                seen[sig] += 1
                chat(f"  ↻ {kind} again: told to use the result it has", "Comment")
                messages.append({"role": "user", "content": "System: you already ran exactly this; its result is above. "
                                 "Use it: run a different action, or declare_result now."})
                if seen[sig] >= 3:
                    return "FAILED", "The LLM kept repeating " + kind + "; its last results are in the GML8 pane."
                continue
            seen[sig] = 1
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
            chat(f"    → {clip(first, 240)}" + (f"  (+{evidence.count(chr(10))} lines)" if "\n" in evidence else ""),
                 "ErrorMsg" if evidence.startswith(("Error", "System: denied", "✘")) else "Comment")
            messages.append({"role": "user", "content": f"[RESULT of {kind}]\n{evidence[:self.args.max_output]}"})
        return "FAILED", f"Stopped after {self.args.max_turns} turns without a result."

    def trim(self, messages):
        budget = self.args.max_history
        total = sum(len(m["content"]) for m in messages)
        i = 2
        while total > budget and i < len(messages) - 2:
            m = messages[i]
            if m["role"] == "user" and len(m["content"]) > 400:
                total -= len(m["content"]) - 400
                m["content"] = m["content"][:400] + " …(older result cut)"
            i += 1

    def act_values(self, e: Entry, a: dict) -> dict:
        """The entry's values (its stage when there is one) with the action's set/unset applied."""
        base = self.staged_values(e, None) or e.values()
        pairs = core.parse_params(base.get("params") or "")
        sets = a.get("set") or {}
        if isinstance(sets, str):
            sets = dict(core.parse_params(sets))
        for flag, val in sets.items():
            flag = flag if str(flag).startswith("-") else ("-" + flag if len(flag) <= 3 else "--" + flag)
            pairs = core.pset(pairs, flag, True if val in (None, True, "") else val)
        for flag in (a.get("unset") or []):
            pairs = core.pset(pairs, flag, False)
        if a.get("params"):
            pairs = core.parse_params(a["params"])
        v = dict(base)
        v["params"] = core.join_params(pairs)
        for k in ("profile", "version", "device"):
            if a.get(k) is not None:
                v[k] = a[k]
        return v

    def do_action(self, kind, a, origin):
        ref = a.get("ref") or a.get("entry") or a.get("id") or a.get("key") or a.get("line")
        if kind == "status":
            return self.status_text()
        if kind == "entries":
            return self.entries_text(a.get("query") or "")
        if kind == "hardware":
            return self.hardware_text()
        if kind == "models":
            return self.models_text(a.get("query") or "")
        if kind == "who":
            return self.iface.net.who() if self.iface.connected else "System: not on the network"
        if kind == "post":
            return "sent" if self.iface.say(str(a.get("text") or ""), to=a.get("to")) else "System: not sent"
        if kind == "ask_agent":
            return self.iface.ask(str(a.get("to") or ""), str(a.get("text") or ""), a.get("wait", 120), self.cancel)
        if kind == "wait_agent":
            return self.iface.wait(str(a.get("from") or a.get("to") or ""), a.get("wait", 120), self.cancel)
        if kind == "local":
            command = str(a.get("command") or "")
            if not self.approve(f"run on this machine: {clip(command, 200)}"):
                return "System: denied by the user"
            p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
            return f"exit {p.returncode}\n{(p.stdout + p.stderr)[-2500:]}"
        if kind == "run_command":
            if not self.approve(f"Ex command {clip(a.get('command'), 160)}"):
                return "System: denied by the user"
            r = self.nvim("ex", command=str(a.get("command") or ""))
            return r.get("out") if r.get("ok") else f"Error: {r.get('err')}"
        if kind == "stop" and (a.get("port") and not ref):
            if not self.approve(f"stop the service on :{a['port']}"):
                return "System: denied by the user"
            return self.stop_ref(None, a.get("port"), why="llm")
        e = self.ref_entry(ref)
        if e is None and ref in (None, ""):
            # no ref: the latest failure for diagnose/logs, else the entry selected in MODEL IDX
            if kind in ("diagnose", "logs", "history"):
                bad = sorted((r for r in self.runs.runs.values() if r.get("state") in ("crashed", "failed", "stalled")),
                             key=lambda r: r.get("ended") or r.get("started") or "")
                e = self.inv.by_key(bad[-1]["key"]) if bad else None
            if e is None:
                sel = self.selected_entry()
                e = sel if sel is not None and sel.launchable else None
        if e is None:
            return f"Error: no entry {ref!r}: give \"ref\" (#<line>, @<name> or part of a model name; entries lists them)"
        if kind == "entry":
            return self.entry_text(e)
        if kind == "history":
            evs = self.mem.events(e.key, 15)
            m = self.mem.entry(e.key)
            return (json.dumps(m, ensure_ascii=False)[:1200] + "\n" if m else "") + \
                "\n".join(f"{when(ev['at'])} {ev['ev']} {clip(json.dumps({k: v for k, v in ev.items() if k not in ('at', 'ev', 'key')}, ensure_ascii=False), 200)}"
                          for ev in evs) or "(no history)"
        if kind == "note":
            self.mem.note(e.key, str(a.get("text") or ""))
            return "noted"
        if kind == "logs":
            r = self.runs.of_key(e.key)
            if not r or not r.get("log"):
                return f"no log for {e.key} (" + ("started outside gml8" if r else "never launched") + ")"
            return "\n".join(core.log_tail(r["log"], int(a.get("lines") or 40)))
        if kind == "diagnose":
            r = self.runs.of_key(e.key)
            out = []
            if r:
                out.append(self.run_line(r))
                for dg in core.diagnose_lines(core.log_tail(r.get("log"), 400)):
                    out.append(f"log: {dg['cause']} — evidence: {dg['evidence']} — fix: {dg['fix']}")
            doc = self.render_idx(e, self.staged_values(e, None))
            cap = doc.lines[next((i for i, l in enumerate(doc.lines) if "CAPACITY" in l), 0):]
            out += [l for l in cap if l.strip()][:12]
            m = self.mem.entry(e.key)
            if m.get("failures"):
                out.append("past failures: " + "; ".join(f"{when(x['at'])} {x['cause']}" for x in m["failures"][-3:]))
            return "\n".join(out)
        if kind == "set_params":
            v = self.act_values(e, a)
            self.stages[e.key] = "\n".join(stage_lines(Entry_from(e, v)))
            if self.sel and self.sel.get("key") == e.key:
                self.emit_idx(stage=self.stages[e.key].split("\n"), force=True)
            return "staged: " + ("; ".join(stage_diff(e, v)) or "no change") + " (not committed)"
        if kind == "commit":
            v = self.staged_values(e, None)
            if not v:
                return "nothing staged for " + e.key
            if not self.approve(f"write {e.key}'s line: {'; '.join(stage_diff(e, v))}"):
                return "System: denied by the user"
            return self.commit(e, v, why="llm")
        if kind == "launch":
            if not self.approve(f"launch {e.key} {e.short}"):
                return "System: denied by the user"
            return self.launch_entry(e, self.staged_values(e, None), origin="llm", wait=True)
        if kind == "relaunch":
            v = self.act_values(e, a)
            diff = stage_diff(e, v)
            if not e.reserved and diff and not self.approve(f"change the NAMED entry {e.key} for this run: {'; '.join(diff)}"):
                return "System: denied by the user"
            if not self.approve(f"relaunch {e.key}" + (f" with {'; '.join(diff)}" if diff else "")):
                return "System: denied by the user"
            if diff:
                self.stages[e.key] = "\n".join(stage_lines(Entry_from(e, v)))
            return self.relaunch(e, v if diff else None, origin="llm", wait=True)
        if kind == "stop":
            r = self.runs.of_key(e.key)
            if not self.approve(f"stop {e.key}" + (" (started outside gml8)" if r and r.get("origin") == "foreign" else "")):
                return "System: denied by the user"
            return self.stop_ref(e.key, why="llm")
        return f"Error: unknown action {kind}"


def Entry_from(e: Entry, v: dict) -> Entry:
    x = Entry(e.line, e.raw)
    x.__dict__.update({k: getattr(e, k) for k in ("kind", "section", "slot", "series", "key", "comment")})
    x.model, x.profile, x.params = v.get("model") or e.model, v.get("profile") or "", v.get("params") or ""
    x.version, x.device, x.extra = v.get("version") or "", v.get("device") or "", v.get("extra") or {}
    return x


def autopilot_allows() -> bool:
    try:
        import yaml
        data = yaml.safe_load(open(os.path.expanduser("~/echo-home/autopilot.yaml"))) or {}
        v = (data.get("gml8") or {}).get("allow")
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
    agent.running = False  # the services keep running: they are detached
    agent.runs.save()
    agent.iface.net.close()
    emit({"t": "exit"})


def main():
    p = argparse.ArgumentParser(description="gml8: the operator of this machine's llama.cpp services (backend of gml8.lua)")
    p.add_argument("--inventory", default=os.environ.get("GML8_INVENTORY") or core.default_inventory())
    p.add_argument("--series", default=core.default_series())
    p.add_argument("--memory", default=os.environ.get("GML8_HOME") or core.DEFAULT_MEMORY)
    p.add_argument("--no-merge", dest="merge", action="store_false", help="do not merge the series into the inventory at start")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between two looks at the services")
    p.add_argument("--probe-timeout", type=float, default=2.0)
    p.add_argument("--stall-after", type=float, default=30.0, help="seconds an up service may stop answering before it is stalled")
    p.add_argument("--no-heal", dest="heal", action="store_false", help="do not relaunch a service that died after it was up")
    p.add_argument("--no-troubleshoot", dest="troubleshoot", action="store_false",
                   help="do not ask the LLM to diagnose a crash by itself")
    p.add_argument("--llm", default="", help="the service that powers gml8: a port, #line or @name (default: the first up)")
    p.add_argument("--think", dest="think", action="store_true", default=True,
                   help="the LLM thinks before it answers; its reasoning shows in the LLM pane (default; ===T toggles)")
    p.add_argument("--no-think", dest="think", action="store_false", help="no thinking: faster, shorter turns")
    p.add_argument("--auto", action="store_true", help="auto-approve (also autopilot.yaml gml8.allow)")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--max-output", type=int, default=2500)
    p.add_argument("--max-history", type=int, default=16000, help="chars of conversation kept for the LLM")
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("GML8_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    g.add_argument("--net-readonly", action="store_true")
    args = p.parse_args()

    agent = Agent(args)
    chat(f"gml8 · the llama.cpp services of {core.hostname()}", "Title")
    chat(f"   inventory {tilde(args.inventory)} · series {tilde(args.series)}", "Comment")
    if args.merge:
        try:
            res = core.merge_series(args.inventory, args.series, agent.mem)
            chat("🧬 " + core.merge_text(res) + (f" · backup in {tilde(agent.mem.bak_dir)}" if res["changed"] else ""),
                 "Comment")
            if res["changed"]:
                emit({"t": "req", "id": 0, "op": "reload", "path": args.inventory})
        except Exception as e:
            chat(f"✘ series merge failed: {e}", "ErrorMsg")
    agent.inv.load(force=True)
    ents = agent.inv.entries()
    chat(f"   {len([e for e in ents if e.reserved])} reserved · {len([e for e in ents if not e.reserved])} named entries · "
         f"memory {tilde(agent.mem.root)}", "Comment")
    active = agent.runs.active()
    if active:
        chat(f"   adopting {len(active)} run{'s' if len(active) > 1 else ''} from before: "
             + ", ".join(f"{r['key']} :{r['port']}" for r in active), "Comment")
    chat("Select an entry in INVENTORY (MODEL IDX follows the cursor) · gml8w / L launches it · ===h help", "Comment")
    logw("LLM — gml8's own LLM, powered by the first service that answers", "Title")
    iface_log("IFACE — Intelligence Network traffic and decisions (shown while joined)", "Title")
    import gml8_iface
    if gml8_iface.INET_ERROR:
        iface_log("⚠️ Intelligence Network driver not loaded: " + gml8_iface.INET_ERROR, "ErrorMsg")
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.op_worker, daemon=True).start()
    threading.Thread(target=agent.watcher, daemon=True).start()
    threading.Thread(target=lambda: [agent.hw.devices(core.server_bin(x)) for x in core.hw_builds()],
                     daemon=True).start()
    if args.net_name:
        agent.iface.join()
    agent.emit_state()
    reader(agent)


if __name__ == "__main__":
    main()
