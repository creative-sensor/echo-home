#!/usr/bin/env python
"""
mksk: the make-sketch planning agent. Headless backend of mksk.lua (same folder).

Cloned from neos.py (same output bridge, protocol, approvals, steering and task loop) and trimmed to planning:
instead of operating the editor, mksk keeps a plan file in the make-sketch format (mksk--<plan_name>--YYYY-MM-DD.md,
~/echo-home/Documents/specs/make-sketch.md) with the user: plan updates and questions, by the user's request.
Only once joined to the Intelligence Network does it also hand targets to other agents and record the progress
they report.

Boundaries
- The plan is only ever read and changed through the built-in Lua functions of mksk_plan.lua ("plan" requests);
  this process never opens the plan file itself.
- At start: the plan named by --plan, else the most up-to-date mksk--*--YYYY-MM-DD.md in --dir, else a zero plan.
- Edits are staged. Nothing reaches the file until the user approves the diff, and then only today's file is
  written; changes made for the network wait in a queue until the user reviews them (===p).
- The conversation stays in the LLM context from start to quit (old action results are shortened, --history-chars).

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"style","id":N,"fg":..,"bg":..,"bold":b,"italic":b,"underline":b,"reverse":b}
  {"t":"w","b":"chat"|"log"|"iface","s":[[text, style id], ...]}     append text
  {"t":"clear","b":"chat"|"log"|"iface"}
  {"t":"state", busy, net_busy, phase, model, approval:{kind,prompt}|null, steer:[..], tasks:[..],
                plan, staged, deferred, net:{joined,connected,name,hub,network,state,members}}
  {"t":"req","id":N,"op":"plan","fn":<mksk_plan.lua function>,"args":{..}} | {"t":"req","id":N,"op":"context"|"open_file"}
  {"t":"input","text":..}      put text in the input line
  {"t":"show"}                 open/focus the float (user input needed)
  {"t":"notify","text":..,"level":"info"|"warn"}   shown when the float is hidden
  {"t":"exit"}

nvim -> backend
  {"t":"input","text":..,"sel":{file,start,end,text}|null,"ctx":{file,line,col,text}|null}
  {"t":"res","id":N,"ok":b,"out":..,"err":..}
  {"t":"net","op":"join","name","purpose","hub","network"} | {"t":"net","op":"leave"} | {"t":"net","op":"say","text"}
  {"t":"interrupt"} | {"t":"cancel"} | {"t":"clear","b":..} | {"t":"quit"}
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

# Claim the real stdout for the protocol before anything can print to it.
_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
for _stream in (sys.stdin, sys.stderr):  # the protocol is UTF-8 whatever the Windows code page is
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import requests  # noqa: E402
from openai import OpenAI  # noqa: E402

ARGS: Optional[argparse.Namespace] = None
CLIENT: Optional[OpenAI] = None
MODEL_NAME: Optional[str] = None

# ==========================================
# 1. ANSI -> NVIM HIGHLIGHT BRIDGE (as neos.py)
# ==========================================
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_BASIC = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5"]
_BRIGHT = ["#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]
_CTRL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[@-ln-~]|\x1b(?!\[[0-9;]*m)|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")


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
    """Batches output into ~30 fps protocol writes and routes request/response pairs."""

    FLUSH_INTERVAL = 0.03
    BUFS = ("chat", "log", "iface")

    def __init__(self):
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.pending: List[dict] = []
        self.sgr: Dict[str, SgrState] = {k: SgrState() for k in self.BUFS}
        self.styles: Dict[tuple, int] = {}
        self.dirty = True
        self.running = True
        self.state_fn: Callable[[], Optional[dict]] = lambda: None
        self._req_id = 0
        self._waiting: Dict[int, dict] = {}

    def emit(self, msg: dict):
        with self.lock:
            self.pending.append(msg)

    def mark_dirty(self):
        self.dirty = True

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

    def write(self, buf: str, text: str):
        if not text:
            return
        text = _CTRL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n")).replace("\t", "    ")
        with self.lock:
            state = self.sgr[buf]
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
            if last and last["t"] == "w" and last["b"] == buf:
                last["s"].extend(segs)
            else:
                self.pending.append({"t": "w", "b": buf, "s": segs})

    def clear(self, buf: str):
        with self.lock:
            self.sgr[buf].reset()
            self.pending.append({"t": "clear", "b": buf})

    # ----- requests into nvim (never from the stdin reader thread: it must stay free to read the answer) -----
    def request(self, op: str, timeout: float, cancel: Optional[threading.Event] = None, **params) -> dict:
        with self.lock:
            self._req_id += 1
            rid = self._req_id
            slot = {"event": threading.Event(), "res": None}
            self._waiting[rid] = slot
            self.pending.append(dict(params, t="req", id=rid, op=op))
        deadline = time.time() + timeout
        try:
            while not slot["event"].wait(0.05):
                if cancel is not None and cancel.is_set():
                    return {"ok": False, "err": "cancelled by user"}
                if time.time() > deadline:
                    return {"ok": False, "err": f"no answer from Neovim after {timeout:.0f}s (is it blocked?)"}
                if not self.running:
                    return {"ok": False, "err": "neovim went away"}
            return slot["res"] or {"ok": False, "err": "empty response"}
        finally:
            with self.lock:
                self._waiting.pop(rid, None)

    def resolve(self, msg: dict):
        with self.lock:
            slot = self._waiting.get(msg.get("id"))
        if slot:
            slot["res"] = msg
            slot["event"].set()

    # ----- output -----
    def flush(self):
        with self.out_lock:
            with self.lock:
                batch, self.pending = self.pending, []
            if self.dirty:
                self.dirty = False
                st = self.state_fn()
                if st:
                    batch.append(st)
            for msg in batch:
                _PROTO.write(json.dumps(msg, ensure_ascii=False) + "\n")
            _PROTO.flush()

    def flusher(self):
        while self.running:
            time.sleep(self.FLUSH_INTERVAL)
            try:
                self.flush()
            except (BrokenPipeError, ValueError, OSError):
                self.running = False


BRIDGE = Bridge()

RST, BOLD, DIM = "\033[0m", "\033[1m", "\033[90m"
RED, GREEN, YELLOW, CYAN, MAG = "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[35m"
_HL = {"ErrorMsg": RED, "WarningMsg": YELLOW, "Comment": DIM, "Title": BOLD, "Special": MAG,
       "DiagnosticOk": GREEN, "String": GREEN}


def chat(text: str = ""):
    BRIDGE.write("chat", str(text) + "\n")


def logw(text: str = ""):
    BRIDGE.write("log", str(text) + "\n")


def ilog(text: str = "", hl: Optional[str] = None):
    """The IFACE pane (mksk_iface.py logs with highlight-group names)."""
    BRIDGE.write("iface", f"{_HL[hl]}{text}{RST}\n" if hl in _HL else f"{text}\n")


def dim(s: str) -> str:
    return f"{DIM}{s}{RST}"


def short(s, n: int = 300) -> str:
    """One line, at most n chars."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def clip(text: str, limit: int) -> str:
    """Keep head and tail of long text (errors usually live at the end)."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars omitted] ...\n{text[-tail:]}"


def parse_llm_json(raw_text: str) -> Optional[dict]:
    clean = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw_text or "", flags=re.DOTALL)
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?", "", clean).strip()
    for cand in (clean, clean[clean.find("{"):clean.rfind("}") + 1] if "{" in clean else ""):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    for start in [m.start() for m in re.finditer(r"\{", clean)]:  # first balanced object amid prose
        depth, in_str, esc = 0, False, False
        for i in range(start, len(clean)):
            ch = clean[i]
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
                        obj = json.loads(clean[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
    return None


# ==========================================
# 2. LLM (as neos.py; reached again on each request when it was down at start)
# ==========================================
def get_model_name(host: str, port: int) -> Optional[str]:
    err: Exception = RuntimeError("no model listed")
    for endpoint in ("/models", "/v1/models"):
        try:
            r = requests.get(f"http://{host}:{port}{endpoint}", timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("models") and "name" in data["models"][0]:
                return data["models"][0]["name"]
            if data.get("data") and "id" in data["data"][0]:
                return data["data"][0]["id"]
        except Exception as e:
            err = e
    print(f"mksk: cannot reach the LLM at http://{host}:{port} ({err})", file=sys.stderr)
    return None


def ensure_model() -> str:
    global MODEL_NAME
    if not MODEL_NAME:
        MODEL_NAME = get_model_name(ARGS.host, ARGS.port)
        BRIDGE.mark_dirty()
    if not MODEL_NAME:
        raise RuntimeError(f"cannot reach the LLM at http://{ARGS.host}:{ARGS.port} (start it, then ask again)")
    return MODEL_NAME


def json_call(system: str, user: str) -> Optional[dict]:
    """One non-streamed call, for the network gate (mksk_iface.py)."""
    model = ensure_model()
    r = CLIENT.chat.completions.create(model=model, temperature=0.0, messages=[
        {"role": "system", "content": system}, {"role": "user", "content": user}])
    return parse_llm_json(r.choices[0].message.content or "")


# ==========================================
# 3. AGENT
# ==========================================
BASE_PROMPT = """You are mksk (make-sketch), a planning agent working with the human user inside their Neovim. You answer in a floating chat window. You draft and refine plans with the user, answer their questions about the plan, and keep the plan file up to date.

THE PLAN FILE (make-sketch format)
- One file per day in the user's folder: mksk--<plan_name>--YYYY-MM-DD.md. You work on the newest. Every approved change is written to
  TODAY's file (the first approved change of a day creates it from the newest one); older files are never changed. This is done for you.
- A zero plan (there was no plan file in the folder) has no name yet. Give it one with name_plan (short kebab-case, from its goal) before
  you declare_result with staged edits.
- A file holds one or more sub-documents separated by a '---' line. Each has three sections:
  # OVERVIEW  "key: value" lines: name (id of the sub-document), goal, resume (ONE line telling the next agent where to pick up), updated.
  # TARGET    a flat checklist table: Wx | target | description | status | agent.   status is TODO, WIP or DONE.
  # DAG       a pseudo Makefile: "target: prerequisite ..." gives the hierarchy and the order of execution; the comment lines under a
              target are free-style notes (input, output, storage, primary steps, deadline, pending, job id, files created, memo ...).
              A prerequisite in another sub-document is written name/target.
- Weights: w is the weight of a target's own task (complexity, magnitude, priority folded into one number). W is the whole weight:
  w plus the w of every target it depends on, directly or not, each counted once. You only ever set w; W, the "[W,w]" display and the
  row order (W descending) are recomputed for you after every edit.
- DONE targets and their notes are never deleted. Evidence of work (files created, job ids, commits, memos) goes into the target's notes.

HOW YOU WORK
1. Each turn output ONE JSON action, then read its result. [PLAN] in the last message is always the current working copy, your staged edits included.
2. Edit actions only STAGE changes; nothing is written to the file by them. When you call declare_result with staged edits, the user is shown the
   diff and answers yes, no, or types what to change (you then get that text and continue with the edits still staged). So write "reply" as a
   proposal ("I staged ..."), and never claim something was saved unless a System line says so.
3. Planning: turn the user's intent into targets with a clear description, an own weight, prerequisites and notes (input, output, primary steps,
   deadline ...). Target names are short snake_case. Prefer several concrete targets to one vague one.
4. Be proactive, but ask: once you staged what the user asked for, look at what the plan still lacks (a missing prerequisite, a verification
   target, inputs or outputs, a deadline, a risk, an outdated resume line) and offer the most useful additions with "suggest"
   (2 to 6 concrete options). Stage only what the user selects. Skip this for trivial edits (a status change, a typo), and do not offer again what
   the user already declined in this conversation.
5. When a request is ambiguous (which sub-document, which target, what weight, what deadline) ask with "suggest" instead of guessing.
6. Whenever a status changes, keep that sub-document's "resume" line true (set_overview, key "resume").
7. A question about the plan (what is next, what blocks a target, how heavy something is, where work stands) is answered from [PLAN] with
   declare_result alone: stage nothing for a question.
8. The whole conversation since startup is in your context: build on earlier decisions and do not re-ask what was settled.
   Messages starting with "User Steering Update" come from the user while you work and override earlier plans.
   [SELECTION] in a request is lines the user selected (usually rows of the plan file): "these", "this" refer to them.
9. Task assignment is not part of your work unless you are on the Intelligence Network (a NETWORK section is then added below this
   prompt). Without it, write a name into the agent column only when the user explicitly asks for that edit.
10. Finish every request with declare_result; "reply" is short and concrete (what you staged, found, or still need to know).
11. Output ONLY one JSON object, no markdown around it.

ACTIONS — "action_type" is exactly one of the names below; all other fields sit at the top level of the same object.
"sub" may be left out when the plan has one sub-document or the target name is unique in the plan.
Plan files:
  {"action_type":"list_plans"}
  {"action_type":"open_plan","name":"db-migration"}                  (an existing plan, or a new name for a separate new plan)
  {"action_type":"name_plan","name":"db-migration"}                  (names the zero plan or a new plan before its first save)
Staged edits:
  {"action_type":"add_sub","name":"schema-migration","goal":"..","resume":"nothing started"}
  {"action_type":"set_overview","sub":"..","key":"resume","value":".."}                 (key: goal, resume, name, or any other word)
  {"action_type":"add_target","sub":"..","target":"dump_restore","description":"..","w":4,"deps":["inventory"],"notes":["output: ..","deadline: .."]}
  {"action_type":"edit_target","target":"dump_restore","status":"WIP","agent":"memphia"}   (any of: description, status, agent, w, deps, rename)
  {"action_type":"set_weights","weights":{"inventory":2,"cutover":3}}                   (own weights w of several targets)
  {"action_type":"note","target":"dump_restore","key":"storage","value":"/mnt/backup/x.dump"}   (sets or replaces "# key: value"; a list value becomes sub-items)
  {"action_type":"note","target":"dump_restore","text":"restore needs pg_trgm first"}          (appends a free line)
  {"action_type":"note","target":"dump_restore","key":"pending","remove":true}
  {"action_type":"remove_target","target":".."}                                        (never a DONE target)
Looking closer:
  {"action_type":"show_target","target":".."}       (one target in full: what it builds on, what needs it, its notes)
  {"action_type":"validate"}                          (consistency warnings)
  {"action_type":"diff"}                              (what is staged so far)
Asking the user:
  {"action_type":"suggest","question":"..","options":["..",".."]}    (the user picks numbers, all, none, or answers freely; without options it is an open question)
Finish:
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
Every action also takes "reason": one short sentence why.
"""

# Added to the system prompt only while mksk is connected to the Intelligence Network (<NAME>, <NETWORK> filled in).
NET_PROMPT = """
NETWORK — you are on the Intelligence Network <NETWORK> as @<NAME>; [NETWORK] in the live state lists the members.
- Task assignment is now part of your work: "assign" hands a target to an agent; after the user approves, the plan records the agent and
  the target's brief is sent to it. While planning you may also suggest owners for unassigned targets among the members.
- Requests marked [Network request ...] come from other agents: when one reports progress, record it (status, and notes for the evidence:
  files, job ids, memo). Changes made for the network are queued for the user's approval, so tell the reporter it is recorded and waits
  for approval. "suggest" and "assign" are not available while serving a network request.
Network actions:
  {"action_type":"who"}
  {"action_type":"assign","target":"..","to":"memphia","message":"<optional extra instruction>"}
  {"action_type":"post","text":"..","to":["name"]}                   (to optional = broadcast)
  {"action_type":"ask_agent","to":"memphia","text":"..","wait":120}
  {"action_type":"wait_agent","from":"memphia","wait":120}
"""

HELP_TEXT = f"""{BOLD}mksk commands{RST}
  <text>          idle: new request │ busy: steering message for the next turn
  !<text>         steer and interrupt the current LLM generation now
  +<text>         queue a request to run after the current one
  ===p            review the plan changes agents reported on the network (queued until you approve)
  ===s            plan status: summary, in progress, ready to start, warnings
  ===d            saved | proposed plan, side by side (also :MkskDiff)
  ===x            cancel the running request      ====  clear queues
  ===r            forget the conversation (start planning afresh)
  ===c            clear the chat                   ===h  this help
  ===n            network status   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
{BOLD}Answers{RST}
  staged edits:   y apply │ n drop │ v side by side │ or type what to change
  suggestions:    numbers (1,3) │ all │ none │ w skip them and write what is staged │ or answer in your own words
                  (<CR> on an option line picks it)
{BOLD}Keys{RST} (in the float)  C-x interrupt │ C-c cancel/clear │ F2 log │ F8 network │ M-h/j/k/l dock, M-c center, M-f full, M-b peek │ q/Esc hide │ ↑/↓ history"""

EDITS = {"add_sub", "set_overview", "add_target", "edit_target", "set_weights", "note", "remove_target"}
NET_ACTIONS = {"who", "assign", "post", "ask_agent", "wait_agent"}
ACTION_NAMES = ["list_plans", "open_plan", "name_plan", *sorted(EDITS), "show_target", "validate", "diff", "suggest",
                *sorted(NET_ACTIONS), "declare_result"]
EDIT_FIELDS = {
    "add_sub": ("name", "goal", "resume"),
    "set_overview": ("sub", "key", "value"),
    "add_target": ("sub", "target", "description", "w", "deps", "notes", "status", "agent"),
    "edit_target": ("sub", "target", "description", "status", "agent", "w", "deps", "rename"),
    "set_weights": ("sub", "weights"),
    "note": ("sub", "target", "key", "value", "text", "remove"),
    "remove_target": ("sub", "target"),
}
PLAN_FILE = re.compile(r"(?:^|[\\/])mksk--.+--\d{4}-\d\d-\d\d\.md$")


def normalize_action(act: dict) -> dict:
    """Accept near-misses small models produce: "action":"x", "edit:add_target", {"action_type":"edit","add_target":{..}}."""
    kind = str(act.get("action_type") or act.get("action") or "").strip()
    if kind in ACTION_NAMES:
        return dict(act, action_type=kind)
    for sep in (":", ".", "/", " "):
        tail = kind.rsplit(sep, 1)[-1].strip()
        if tail in ACTION_NAMES:
            return dict(act, action_type=tail)
    for name in ACTION_NAMES:
        if name in act:
            inner = act[name]
            out = {k: v for k, v in act.items() if k not in (name, "action_type", "action")}
            if isinstance(inner, dict):
                out.update(inner)
            out["action_type"] = name
            return out
    return act


class Cancelled(Exception):
    pass


class PlanError(Exception):
    pass


class MkskAgent:
    def __init__(self):
        self.lock = threading.RLock()
        self.tasks: deque = deque()
        self.steering: deque = deque()
        self.wake = threading.Event()
        self.cancel_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.running = True
        self.busy = False
        self.phase = "starting"
        self.current: Optional[dict] = None   # the running task
        self.approval: Optional[dict] = None  # {"kind", "prompt", "event", "answer"} while the user is asked something
        self.history: List[dict] = []         # the conversation since startup: {"role", "content", "k"}
        self.trim_told = False
        self.ops: List[Tuple[str, dict]] = []  # edits staged by the running request (replayable)
        self.write_now = False                 # the user answered a PICK with w
        self.deferred: deque = deque()         # changes made for the network, waiting for the user
        self.plan_info: dict = {}              # last P.state()
        from mksk_iface import IFace
        self.iface = IFace(self, ARGS, json_call, ilog)

    # ---------- UI thread API ----------
    def state(self) -> dict:
        with self.lock:
            ap, cur = self.approval, self.current
            return {
                "t": "state", "busy": self.busy, "net_busy": bool(cur and cur.get("origin")), "phase": self.phase,
                "model": MODEL_NAME, "approval": {"kind": ap["kind"], "prompt": ap["prompt"]} if ap else None,
                "steer": list(self.steering), "tasks": [t["text"] for t in self.tasks],
                "plan": (self.plan_info.get("name") or "zero plan") if self.plan_info.get("open") else None,
                "staged": bool(self.ops), "deferred": len(self.deferred), "net": self.iface.summary(),
            }

    def submit_task(self, text: str, sel: Optional[dict] = None, ctx: Optional[dict] = None,
                    origin: Optional[dict] = None, kind: str = "task", front: bool = False):
        task = {"kind": kind, "text": text, "sel": sel, "ctx": ctx, "origin": origin}
        with self.lock:
            if front:
                self.tasks.appendleft(task)
            else:
                self.tasks.append(task)
        self.wake.set()
        BRIDGE.mark_dirty()

    def submit_steer(self, text: str, interrupt: bool = False):
        with self.lock:
            self.steering.append(text)
        if interrupt:
            self.interrupt_event.set()
        BRIDGE.mark_dirty()

    def steerable(self) -> bool:
        """Steering only reaches a running request of the user's own (not a network request, not a review)."""
        cur = self.current
        return bool(self.busy and cur and cur["kind"] == "task" and not cur.get("origin"))

    def clear_queues(self) -> Tuple[int, int]:
        with self.lock:
            n = (len(self.steering), len(self.tasks))
            self.steering.clear()
            self.tasks.clear()
        BRIDGE.mark_dirty()
        return n

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.interrupt_event.set()
            chat("\033[41;97m cancel requested \033[0m")

    def interrupt(self):
        if self.busy:
            self.interrupt_event.set()
            chat(f"{YELLOW}⏹ interrupt requested{RST}")

    def shutdown(self):
        self.running = False
        self.cancel()
        self.wake.set()

    def set_phase(self, phase: str):
        self.phase = phase
        BRIDGE.mark_dirty()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    # ---------- for the iFace (mksk_iface.py) ----------
    def submit(self, text: str, origin: Optional[dict] = None):
        self.submit_task(text, origin=origin)

    def emit_state(self):
        BRIDGE.mark_dirty()

    def has_net_work(self) -> bool:
        with self.lock:
            return bool(self.current and self.current.get("origin")) or any(t.get("origin") for t in self.tasks)

    def describe_busy(self) -> str:
        with self.lock:
            cur, n = self.current, len(self.tasks)
        return (f"busy with: {short(cur['text'], 160)}" if cur else "idle") + f"; {n} queued"

    def digest(self) -> str:
        """The plan in one line, for the network gate."""
        st = self.plan_state(cancellable=False)
        if not st.get("open"):
            return "(no plan is open)"
        rows = [f"{t['sub']}/{t['target']} [{t['status']}{', ' + t['agent'] if t.get('agent') else ''}]"
                for t in st.get("targets") or []]
        return f"plan {st.get('name')}: " + ("; ".join(rows) or "(no targets yet)")

    # ---------- asking the user (the answer comes from the input line) ----------
    def ask_user(self, kind: str, prompt: str, text: str = "") -> str:
        """kind: diff | assign | review | choice | question. Blocks the worker until the user answers."""
        req = {"kind": kind, "prompt": prompt, "event": threading.Event(), "answer": None}
        with self.lock:
            self.approval = req  # set before the question shows, so an instant answer finds it
        if text:
            chat(text)
        BRIDGE.emit({"t": "show"})
        self.set_phase("waiting for you")
        try:
            while not req["event"].wait(0.1):
                if self.cancel_event.is_set() or not self.running:
                    break
        finally:
            with self.lock:
                self.approval = None
            BRIDGE.mark_dirty()
        self._check_cancel()
        self.set_phase("working")
        return (req["answer"] or "").strip()

    def answer_approval(self, text: str):
        with self.lock:
            req = self.approval
        if req:
            req["answer"] = text
            req["event"].set()

    # ---------- the plan (built-in functions of mksk_plan.lua) ----------
    def plan(self, fn: str, cancellable: bool = True, timeout: Optional[float] = None, **params):
        args = {k: v for k, v in params.items() if v is not None}
        if fn != "state":
            logw(f"{CYAN}→ plan.{fn}{RST} {json.dumps(args, ensure_ascii=False)[:2000]}")
        res = BRIDGE.request("plan", timeout or ARGS.cmd_timeout, self.cancel_event if cancellable else None,
                             fn=fn, args=args)
        if cancellable:
            self._check_cancel()
        if fn != "state":
            body = res.get("out") if res.get("ok") else res.get("err")
            logw(f"{CYAN}← plan.{fn}{RST} ok={res.get('ok')} {str(body if body is not None else '')[:1500]}")
        if not res.get("ok"):
            raise PlanError(str(res.get("err") or "plan function failed"))
        return res.get("out")

    def edit(self, fn: str, **params) -> str:
        """A staging edit: remembered so it can be replayed when the user reviews it later."""
        args = {k: v for k, v in params.items() if v is not None}
        out = self.plan(fn, **args)
        self.ops.append((fn, args))
        BRIDGE.mark_dirty()
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    def plan_state(self, cancellable: bool = True) -> dict:
        try:
            st = self.plan("state", timeout=10, cancellable=cancellable)
        except PlanError as e:
            st = {"open": False, "error": str(e)}
        if isinstance(st, dict):
            self.plan_info = st
            BRIDGE.mark_dirty()
        return self.plan_info or {"open": False}

    def drop_staged(self):
        if self.ops:
            try:
                self.plan("discard", timeout=10, cancellable=False)
            except PlanError:
                pass
            self.ops = []
        BRIDGE.mark_dirty()

    # ---------- reviewing staged edits ----------
    def show_diff(self, d: dict):
        head = f"  proposed change → {d.get('to')}"
        if d.get("new_file"):
            head += "  (new snapshot" + (f", from {os.path.basename(d['from'])})" if d.get("from") else ")")
        chat(f"{MAG}{head}{RST}")
        lines = [l for l in str(d.get("text") or "").split("\n") if l]
        out = []
        for l in lines[:ARGS.diff_lines]:
            colour = CYAN if l.startswith("@@") else GREEN if l.startswith("+") else RED if l.startswith("-") else ""
            out.append(f"  {colour}{l}{RST}" if colour else f"  {l}")
        if out:
            chat("\n".join(out))
        if len(lines) > ARGS.diff_lines:
            chat(dim(f"  … {len(lines) - ARGS.diff_lines} more line(s): v shows everything side by side"))
        chat(dim(f"  +{d.get('added', 0)} −{d.get('removed', 0)} line(s)"))

    def confirm(self, kind: str, question: str, prompt: str) -> str:
        """Ask about the staged diff. Returns "y", "n" or the user's own words."""
        yes = "y save and send" if kind == "assign" else "y apply"
        while True:
            ans = self.ask_user(kind, prompt, f"{YELLOW}❓ {question}{RST}  "
                                + dim(f"{yes} · n drop · v side by side · or type what to change"))
            low = ans.lower()
            if low in ("v", "view", "d", "diff"):
                try:
                    chat(dim("  " + str(self.plan("show_diff"))))
                except PlanError as e:
                    chat(f"{RED}  cannot show the diff: {e}{RST}")
                continue
            if low in ("y", "yes", "ok", "apply"):
                return "y"
            if low in ("n", "no", ""):
                return "n"
            return ans

    def commit(self) -> str:
        r = self.plan("commit", cancellable=False)
        self.ops = []
        self.plan_state(cancellable=False)
        text = r.get("text") if isinstance(r, dict) else str(r)
        chat(f"\033[1;32m✔{RST} {text}")
        return text

    def review(self, origin: Optional[dict]) -> Tuple[str, str]:
        """Settle the staged edits of the running request: saved | discarded | deferred | revise | none."""
        try:
            d = self.plan("diff")
        except PlanError as e:
            chat(f"{RED}❌ {e}{RST}")
            self.drop_staged()
            return "discarded", str(e)
        if not d.get("changed"):
            self.drop_staged()
            return "none", ""
        if origin:  # the network never writes the file: park the change until the user looks at it
            self.deferred.append({"from": origin.get("from"), "id": origin.get("id"), "text": origin.get("text") or "",
                                  "ops": list(self.ops)})
            self.drop_staged()
            chat(f"{YELLOW}⏸ change for @{origin.get('from')} queued for your approval — ===p reviews it "
                 f"({len(self.deferred)} waiting){RST}")
            BRIDGE.emit({"t": "notify", "level": "warn",
                         "text": f"@{origin.get('from')} reported progress: {len(self.deferred)} plan change(s) wait for your approval (===p)"})
            return "deferred", ""
        self.show_diff(d)
        ans = self.confirm("diff", "apply this to the plan file?", "APPLY? y/n/v")
        if ans == "y":
            return "saved", self.commit()
        if ans == "n":
            self.drop_staged()
            chat(f"{YELLOW}✖ not saved: the staged edits were dropped{RST}")
            return "discarded", ""
        return "revise", ans

    def review_deferred(self):
        if not self.deferred:
            chat(dim("nothing is waiting for review"))
            return
        while self.deferred:
            item = self.deferred[0]
            chat(f"\n{MAG}⏸ reported by @{item['from']} (#{item['id']}):{RST} {short(item['text'], 300)}")
            try:
                for fn, params in item["ops"]:
                    self.plan(fn, **params)
                self.ops = list(item["ops"])
                d = self.plan("diff")
            except PlanError as e:
                self.ops = list(item["ops"])
                self.drop_staged()
                self.deferred.popleft()
                chat(f"{RED}  it no longer fits the plan and was dropped: {e}{RST}")
                continue
            if not d.get("changed"):
                self.drop_staged()
                self.deferred.popleft()
                chat(dim("  already in the plan"))
                continue
            self.show_diff(d)
            ans = self.confirm("review", "apply this to the plan file?", "APPLY? y/n/v · else later")
            if ans == "y":
                try:
                    self.commit()
                except PlanError as e:
                    self.drop_staged()
                    chat(f"{RED}  cannot save it: {e} — left in the queue{RST}")
                    break
                verdict = "applied to the plan"
            elif ans == "n":
                self.drop_staged()
                chat(f"{YELLOW}✖ dropped{RST}")
                verdict = "declined by the user"
            else:
                self.drop_staged()
                chat(dim(f"  left in the queue ({len(self.deferred)} waiting, ===p to come back)"))
                break
            self.deferred.popleft()
            self.note(f"System: the change reported by @{item['from']} ({short(item['text'], 200)}) was {verdict}.")
            if self.iface.connected:
                self.iface.say(f"Your report (#{item['id']}) was {verdict}.", re=item["id"], to=[item["from"]])
            BRIDGE.mark_dirty()

    # ---------- history (kept from startup to quit) ----------
    def note(self, text: str, k: str = "sys"):
        self.history.append({"role": "user", "content": text, "k": k})

    def system_prompt(self) -> str:
        """Planning only; the network part (task assignment) is added only while mksk is on the Intelligence Network."""
        if not self.iface.connected:
            return BASE_PROMPT
        net = self.iface.net
        return BASE_PROMPT + NET_PROMPT.replace("<NAME>", str(net.name)).replace("<NETWORK>", str(net.network or "network"))

    def messages(self, live: str) -> List[dict]:
        """System prompt + the whole conversation + the live plan. Same-role neighbours are merged (strict chat templates)."""
        out = [{"role": "system", "content": self.system_prompt()}]
        for m in self.history + [{"role": "user", "content": live}]:
            if out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def compact(self, start: int):
        """After a request: shorten its action results, then keep the conversation inside --history-chars by
        forgetting the oldest action steps first (requests, steering, replies and decisions are kept the longest)."""
        for m in self.history[start:]:
            if m["k"] == "res" and len(m["content"]) > ARGS.keep_result:
                m["content"] = m["content"][: ARGS.keep_result] + " …[shortened]"
        size = sum(len(m["content"]) for m in self.history)
        dropped = False
        for kinds in (("res", "act"), ("req", "steer", "final", "sys")):
            i = 0
            while size > ARGS.history_chars and i < len(self.history) - 2:
                if self.history[i]["k"] in kinds:
                    size -= len(self.history.pop(i)["content"])
                    dropped = True
                else:
                    i += 1
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
        if dropped and not self.trim_told:
            self.trim_told = True
            chat(dim(f"  (the conversation outgrew {ARGS.history_chars} characters: the oldest action steps are no longer "
                     "in my context; requests and decisions are kept)"))

    # ---------- LLM ----------
    def stream_llm(self, messages: List[dict]) -> Tuple[str, bool]:
        model = ensure_model()
        self.set_phase("thinking")
        response = CLIENT.chat.completions.create(model=model, messages=messages, temperature=0.0, stream=True)
        output, interrupted, in_reasoning = "", False, False
        try:
            for chunk in response:
                if self.interrupt_event.is_set():
                    interrupted = True
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    if not in_reasoning:
                        BRIDGE.write("log", DIM)
                        in_reasoning = True
                    BRIDGE.write("log", reasoning.replace("\n", "\n" + DIM))
                if delta.content:
                    if in_reasoning:
                        BRIDGE.write("log", RST + "\n")
                        in_reasoning = False
                    output += delta.content
                    BRIDGE.write("log", delta.content)
        finally:
            BRIDGE.write("log", RST + "\n")
            if interrupted:
                try:
                    response.close()
                except Exception:
                    pass
        return output, interrupted

    # ---------- worker ----------
    def boot(self):
        """The plan named by --plan, else the most up-to-date mksk--<plan>--YYYY-MM-DD.md in --dir, else a zero plan."""
        try:
            self.plan("setup", dir=ARGS.dir)
            name = ARGS.plan
            if not name:
                latest = self.plan("latest", dir=ARGS.dir)
                name = latest.get("name") if isinstance(latest, dict) else ""
            chat(f"{BOLD}{self.plan('open', dir=ARGS.dir, name=name or '')}{RST}")
            if not name:
                chat(dim(f"  no mksk--<plan>--<date>.md in {ARGS.dir}: starting from a zero plan — tell me what to plan"))
            others = [f"{p['name']} ({p['latest']})" for p in (self.plan("list", dir=ARGS.dir) or []) if p["name"] != name]
            if others:
                chat(dim("  other plans here: " + ", ".join(others) + " — ask me to switch"))
            st = self.plan_state()
            for w in (st.get("warnings") or [])[:8]:
                chat(f"{YELLOW}  ⚠ {w}{RST}")
        except (PlanError, Cancelled) as e:
            chat(f"{RED}⚠️ {e}{RST}")
        self.set_phase("idle")

    def worker(self):
        self.boot()
        while self.running:
            self.wake.wait(0.2)
            with self.lock:
                task = self.tasks.popleft() if self.tasks else None
                if not self.tasks:
                    self.wake.clear()
            if not task or not self.running:
                continue
            self.cancel_event.clear()
            self.interrupt_event.clear()
            self.write_now = False
            with self.lock:
                self.busy, self.current = True, task
            BRIDGE.mark_dirty()
            cancelled = False
            try:
                if task["kind"] == "review":
                    self.review_deferred()
                else:
                    self.run_request(task)
            except Cancelled:
                cancelled = True
                chat(f"{YELLOW}🛑 request cancelled{RST}")
                self.note("System: the user cancelled that request; nothing staged was saved.")
            except Exception as e:
                chat(f"{RED}❌ {type(e).__name__}: {e}{RST}")
                logw(f"[AGENT ERROR] {type(e).__name__}: {e}")
            finally:
                self.drop_staged()
                with self.lock:
                    left = list(self.steering)
                    self.steering.clear()
                    self.busy, self.current = False, None
                if left and cancelled:
                    chat(dim(f"  dropped {len(left)} steering message(s) with the cancelled request"))
                elif left:  # typed while the request was finishing: it becomes the next request
                    chat(dim("  ↪ your steering came after the request finished: it is the next request"))
                    self.submit_task("\n".join(left), front=True)
                self.set_phase("idle")
                if self.iface.connected:
                    self.iface.set_state(self.iface.idle_state())

    def run_request(self, task: dict):
        text, origin, sel, ctx = task["text"], task.get("origin"), task.get("sel"), task.get("ctx")
        logw(f"\n\033[1;36m▶ REQUEST:\033[0m {text}")
        if origin:
            chat(f"\n{MAG}🌐 network request #{origin.get('id')} from @{origin.get('from')}:{RST} {short(text, 300)}")
        who = f"[Network request #{origin.get('id')} from @{origin.get('from')}] " if origin else ""
        body = f"[REQUEST] {who}{text}"
        if sel:
            body += f"\n[SELECTION] {sel.get('file') or '[No Name]'} lines {sel.get('start')}-{sel.get('end')}:\n{sel.get('text', '')}"
        self.history.append({"role": "user", "content": body, "k": "req"})
        start = len(self.history)
        try:
            status, reply, outcome = self.run_task(text, origin, ctx)
        finally:
            self.compact(start)
        if origin:
            if outcome == "deferred":
                reply = (reply + " " if reply else "") + "(The plan change waits for the user's approval.)"
            self.iface.answer(origin, status, reply)
        else:
            BRIDGE.emit({"t": "notify", "text": f"mksk {status}: {reply[:120]}",
                         "level": "info" if status == "SUCCESS" else "warn"})

    def live_block(self, text: str, origin: Optional[dict], ctx: Optional[dict]) -> str:
        st = self.plan_state()
        parts = [f"[TODAY] {time.strftime('%Y-%m-%d')}"]
        if st.get("open"):
            if not st.get("name"):
                head = "[PLAN] ZERO PLAN · no name and no file yet (name_plan before the first save)"
            else:
                head = f"[PLAN] {st.get('name')} · approved changes go to {st.get('file')}"
                if st.get("is_new"):
                    head += " · NEW PLAN, no file yet"
                elif st.get("new_snapshot"):
                    head += f" (created from {os.path.basename(str(st.get('base_file')))} on the first approval today)"
            others = [p["name"] for p in st.get("plans") or [] if p.get("name") != st.get("name")]
            if others:
                head += " · other plans in the folder: " + ", ".join(others)
            if st.get("dirty"):
                head += " · STAGED EDITS INCLUDED (not in the file until the user approves)"
            body = str(st.get("text") or "") or "(empty: add_sub starts the first sub-document)"
            if len(body) > ARGS.max_plan:
                body = body[: ARGS.max_plan] + "\n… [plan shortened: use show_target for details]"
            parts.append(head + "\n" + body)
            if st.get("warnings"):
                parts.append("[WARNINGS]\n" + "\n".join(f"- {w}" for w in st["warnings"][:20]))
            parts.append("[RESUME] in progress: " + (", ".join(st.get("wip") or []) or "nothing")
                         + " · ready to start (TODO, prerequisites DONE): " + (", ".join(st.get("ready") or []) or "nothing"))
        else:
            plans = st.get("plans") or []
            parts.append(f"[PLAN] none is open. Folder {st.get('dir')}: "
                         + (", ".join(f"{p['name']} ({p['latest']})" for p in plans) or "no plan files yet")
                         + ". open_plan opens one; a new name starts a new plan.")
        if self.deferred:
            parts.append(f"[QUEUED] {len(self.deferred)} change(s) reported on the network wait for the user's approval "
                         "(the user reviews them with ===p)")
        if self.iface.connected:
            parts.append(f"[NETWORK] {self.iface.net.network} as @{self.iface.net.name}; members: {self.iface.net.who()}")
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network ({'private' if origin.get('to') else 'in the room'}). "
                         "The user is not part of this exchange.")
        elif isinstance(ctx, dict) and PLAN_FILE.search(str(ctx.get("file") or "")):
            parts.append(f"[USER POSITION] {ctx.get('file')} line {ctx.get('line')}: {short(ctx.get('text'), 200)}")
        parts.append(f"The request you are working on: {text}")
        return "System (live state, not a new request):\n" + "\n\n".join(parts)

    def take_steering(self):
        with self.lock:
            steers = list(self.steering)
            self.steering.clear()
        for s in steers:
            chat(dim(f"  ↪ steering: {s.splitlines()[0]}"))
            self.history.append({"role": "user", "content": f"User Steering Update: {s}", "k": "steer"})
        if steers:
            BRIDGE.mark_dirty()

    def run_task(self, text: str, origin: Optional[dict], ctx: Optional[dict]) -> Tuple[str, str, str]:
        for turn in range(ARGS.max_turns):
            self._check_cancel()
            logw(f"{CYAN}── turn {turn + 1} ──{RST}")
            self.take_steering()
            self.interrupt_event.clear()
            messages = self.messages(self.live_block(text, origin, ctx))
            if os.environ.get("MKSK_DEBUG_PROMPT"):  # opt-in: overwrite a file with the exact prompt of this turn
                try:
                    with open(os.environ["MKSK_DEBUG_PROMPT"], "w", encoding="utf-8") as f:
                        for msg in messages:
                            f.write(f"===== {msg['role']}\n{msg['content']}\n")
                except OSError:
                    pass
            raw, interrupted = self.stream_llm(messages)
            self._check_cancel()
            if interrupted:
                chat(dim("  ⏹ generation interrupted"))
                self.note("System: The user interrupted this generation turn.", "res")
                continue
            act = parse_llm_json(raw)
            if not act or not (act.get("action_type") or act.get("action")):
                chat(dim("  ⚠ reply was not JSON; retrying"))
                self.history.append({"role": "assistant", "content": raw[-2000:] or "(empty)", "k": "act"})
                self.note("System: Reply with ONLY one JSON object with \"action_type\".", "res")
                continue
            act = normalize_action(act)
            kind = str(act.get("action_type") or "").strip()
            self.history.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False), "k": "act"})

            if kind == "declare_result":
                self.history[-1]["k"] = "final"
                status = "SUCCESS" if str(act.get("status") or "SUCCESS").upper() == "SUCCESS" else "FAILED"
                reply = str(act.get("reply") or act.get("reason") or "").strip()
                if self.ops and not origin and not self.plan_state().get("name"):
                    chat(dim("  ↪ the plan needs a name before it can be saved"))
                    self.note("System: the plan has no name yet, so it cannot be saved. Call name_plan with a short kebab-case "
                              "name taken from its goal, then declare_result again.", "res")
                    continue
                colour = "\033[1;32m" if status == "SUCCESS" else "\033[1;31m"
                chat(f"{colour}◆{RST} {reply}")
                outcome, detail = self.review(origin) if self.ops else ("none", "")
                if outcome == "revise":
                    self.note(f"The user did not approve the staged edits yet and answered: {detail}\n"
                              "Adjust the edits (they are still staged), then declare_result again.", "res")
                    continue
                self.settled(outcome, detail)
                return status, reply, outcome

            label = " ".join(f"{k}={short(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 60)}"
                             for k, v in act.items() if k not in ("action_type", "reason"))
            chat(f"  \033[38;5;112m⚙ {kind}{RST} {label}")
            if act.get("reason"):
                chat(f"  \033[3;90m💭 {short(act['reason'], 200)}{RST}")
            self.set_phase(kind)
            try:
                evidence = self.do_action(kind, act, origin)
            except Cancelled:
                raise
            except PlanError as e:
                evidence = f"Error: {e}"
            except Exception as e:
                evidence = f"Error: {type(e).__name__}: {e}"
            evidence = str(evidence)
            first = evidence.split("\n", 1)[0]
            more = f"  (+{evidence.count(chr(10))} lines)" if "\n" in evidence else ""
            if evidence.startswith(("Error", "System: denied")):
                chat(f"{RED}    ✗ {short(first, 240)}{RST}")
            else:
                chat(dim(f"    → {short(first, 240)}{more}"))
            self.note(f"[RESULT of {kind}]\n{clip(evidence, ARGS.max_output)}", "res")
            if self.write_now:  # "w" at a PICK: no more LLM turns, straight to the write approval
                self.write_now = False
                done = self.write_staged(origin)
                if done:
                    return done
        chat(f"{YELLOW}⚠ max turns reached{RST}")
        self.note(f"System: the request stopped after {ARGS.max_turns} steps; nothing staged was saved.")
        return "FAILED", f"Stopped after {ARGS.max_turns} steps without a result.", "none"

    # ---------- actions (the plan is only ever touched through the built-in Lua functions) ----------
    def do_action(self, kind: str, a: dict, origin: Optional[dict]) -> str:
        if kind in NET_ACTIONS and not self.iface.connected:
            return ("System: denied — mksk is not on the Intelligence Network: task assignment and network actions exist only after "
                    "the user joins it (mkskj). Stay with the plan: updates and questions.")
        if kind not in ("add_sub", "open_plan", "name_plan") and a.get("target") is None and a.get("name") is not None:
            a = dict(a, target=a["name"])  # near-miss: "name" for the target
        if kind in EDITS:
            return self.edit(kind, **{k: a.get(k) for k in EDIT_FIELDS[kind]})
        if kind == "name_plan":
            out = self.plan("name_plan", name=str(a.get("name") or ""))
            self.plan_state()
            return out
        if kind == "list_plans":
            plans = self.plan("list") or []
            return "\n".join(f"- {p['name']}: newest {p['latest']}, {p['snapshots']} snapshot(s)" for p in plans) or "no plan files yet"
        if kind == "open_plan":
            if self.ops:
                return "System: denied — there are staged edits; declare_result first so the user can settle them."
            if origin:
                return "System: denied — network requests cannot switch the plan."
            out = self.plan("open", name=str(a.get("name") or ""))
            self.plan_state()
            return out
        if kind == "show_target":
            return self.plan("brief", sub=a.get("sub"), target=a.get("target"))
        if kind == "validate":
            return "\n".join(f"- {w}" for w in (self.plan("validate") or [])) or "no warnings"
        if kind == "diff":
            d = self.plan("diff")
            return (d.get("text") or "") if d.get("changed") else "nothing is staged"
        if kind == "suggest":
            return self.suggest(a, origin)
        if kind == "assign":
            return self.assign(a, origin)
        if kind == "who":
            return self.iface.net.who() if self.iface.connected else "System: not on the network."
        if kind == "ask_agent":
            text = str(a.get("text") or "").strip()
            if not text:
                return "Error: ask_agent needs \"text\""
            self.set_phase("waiting @" + str(a.get("to")))
            return self.iface.ask(str(a.get("to") or ""), text, a.get("wait") or ARGS.ask_timeout, self.cancel_event,
                                  re=origin.get("id") if origin else None)
        if kind == "wait_agent":
            self.set_phase("waiting @" + str(a.get("from")))
            return self.iface.wait(str(a.get("from") or ""), a.get("wait") or ARGS.ask_timeout, self.cancel_event)
        if kind == "post":
            to = a.get("to") if isinstance(a.get("to"), list) else None
            return "posted" if self.iface.say(str(a.get("text") or ""), to=to) else "System: not sent (not connected or empty)."
        return f"Error: unknown action_type '{kind}'. Use one of: {', '.join(ACTION_NAMES)}"

    def settled(self, outcome: str, detail: str):
        """Tell the LLM how the staged edits of this request ended."""
        if outcome == "saved":
            self.note(f"System: the user APPROVED the staged edits: {detail}.")
        elif outcome == "discarded":
            self.note("System: the user REJECTED the staged edits; they were dropped and the file is unchanged.")
        elif outcome == "deferred":
            self.note("System: the staged edits were queued for the user's approval; they are NOT in the file yet.")

    def write_staged(self, origin: Optional[dict]) -> Optional[Tuple[str, str, str]]:
        """The user answered a PICK with w: add none of the suggestions and settle what is staged right away.
        Returns the request's result, or None when the LLM must act first (name the plan, or revise the edits)."""
        if not self.ops:
            reply = "Suggestions skipped; nothing is staged, so nothing was written."
            chat(f"\033[1;32m◆{RST} {reply}")
            self.history.append({"role": "assistant", "k": "final", "content": json.dumps(
                {"action_type": "declare_result", "status": "SUCCESS", "reply": reply}, ensure_ascii=False)})
            return "SUCCESS", reply, "none"
        if not self.plan_state().get("name"):
            chat(dim("  ↪ the plan needs a name before it can be saved"))
            self.note("System: the user wants to write now, but the plan has no name yet. Call name_plan with a short "
                      "kebab-case name taken from its goal, then declare_result.", "res")
            return None
        chat(f"\033[1;32m◆{RST} suggestions skipped: writing what is staged")
        outcome, detail = self.review(origin)
        if outcome == "revise":
            self.note(f"The user did not approve the staged edits yet and answered: {detail}\n"
                      "Adjust the edits (they are still staged), then declare_result again.", "res")
            return None
        reply = {"saved": f"Suggestions skipped; {detail}.",
                 "discarded": "Suggestions skipped; the staged edits were dropped.",
                 "deferred": "Suggestions skipped; the change waits for approval."}.get(outcome, "Suggestions skipped; nothing changed.")
        self.history.append({"role": "assistant", "k": "final", "content": json.dumps(
            {"action_type": "declare_result", "status": "SUCCESS", "reply": reply}, ensure_ascii=False)})
        self.settled(outcome, detail)
        return "SUCCESS", reply, outcome

    def suggest(self, a: dict, origin: Optional[dict]) -> str:
        """Offer options and let the user choose; nothing is staged here."""
        if origin:
            return "System: denied — the user is not part of a network request; decide from the plan or report what is unclear."
        question = str(a.get("question") or "Which of these should go into the plan?").strip()
        opts = a.get("options") if isinstance(a.get("options"), list) else []
        opts = [str(o).strip() for o in opts if str(o).strip()]
        if not opts:
            return f"The user answered: {self.ask_user('question', 'ANSWER', f'{BOLD}❔ {question}{RST}')}"
        listing = "\n".join(f"   {i}. {o}" for i, o in enumerate(opts, 1))
        ans = self.ask_user("choice", "PICK 1,3/all/none/w", f"{BOLD}❔ {question}{RST}\n{listing}\n"
                            + dim("   pick numbers (1,3) · all · none · w skip and write what is staged · or answer in your own words"))
        low = ans.lower()
        if low in ("w", "write"):
            self.write_now = True
            return "The user chose w: skip the suggestions (add none of them) and write what is staged now."
        if low in ("all", "a", "*", "y", "yes"):
            chosen = list(range(len(opts)))
        elif low in ("none", "n", "no", "0", "-", ""):
            return "The user selected NONE of the options: do not add any of them, and do not offer them again."
        elif re.fullmatch(r"[\d\s,;.&and]+", low) and re.search(r"\d", low):
            chosen = sorted({int(n) - 1 for n in re.findall(r"\d+", low) if 1 <= int(n) <= len(opts)})
            if not chosen:
                return f"The user answered: {ans} (no such option number; ask again if needed)"
        else:
            return f"The user answered in their own words: {ans}"
        rest = [o for i, o in enumerate(opts) if i not in chosen]
        return ("The user selected:\n" + "\n".join(f"- {opts[i]}" for i in chosen)
                + ("\nNot selected (do not add, do not offer again):\n" + "\n".join(f"- {o}" for o in rest) if rest else ""))

    def assign(self, a: dict, origin: Optional[dict]) -> str:
        """Hand a target to an agent on the network: plan change + brief, both after the user's approval."""
        if origin:
            return "System: denied — only the user assigns targets; tell the requester to ask the user."
        if not self.iface.connected:
            return "System: not on the Intelligence Network (the user joins with mkskj or :InetJoin mksk)."
        who = self.iface.member(str(a.get("to") or ""))
        if not who:
            return f"System: @{a.get('to')} is not on the network. Members: {self.iface.net.who()}"
        sub, target = a.get("sub"), a.get("target")
        self.edit("edit_target", sub=sub, target=target, agent=who)
        self.edit("note", sub=sub, target=target, key="assigned", value=f"{time.strftime('%Y-%m-%d')} to @{who}")
        brief = str(self.plan("brief", sub=sub, target=target, reply_to=self.iface.net.name))
        if a.get("message"):
            brief += "\n" + str(a["message"]).strip()
        chat(f"{MAG}  brief for @{who}:{RST}\n" + dim("\n".join("  │ " + l for l in brief.split("\n"))))
        self.show_diff(self.plan("diff"))
        ans = self.confirm("assign", f"assign {target} to @{who} and send this brief?", "SEND? y/n/v")
        if ans == "y":
            self.commit()
            sent = self.iface.say(brief, to=[who])
            self.note(f"System: the user APPROVED: {target} is assigned to @{who} in the file"
                      + ("; the brief was sent." if sent else "; the brief could NOT be sent."))
            return (f"assigned and saved; the brief was sent to @{who}, who reports back on the network later"
                    if sent else f"assigned and saved, but the brief could not be sent to @{who}")
        if ans == "n":
            self.drop_staged()
            return "The user said NO: nothing was assigned or sent, and every staged edit was dropped."
        return f"The user did not approve yet and answered: {ans}\n(The edits are still staged; nothing was sent.)"


# ==========================================
# 4. INPUT ROUTING
# ==========================================
class Router:
    def __init__(self, agent: MkskAgent):
        self.a = agent

    def accept(self, text: str, sel: Optional[dict], ctx: Optional[dict] = None) -> str:
        """"exit" | "ok". Runs on the stdin reader thread: never waits on Neovim here."""
        a, clean = self.a, (text or "").strip()
        if clean == "===x":
            a.cancel() if a.busy else chat(dim("nothing is running"))
            return "ok"
        if a.approval:
            a.answer_approval(clean)
            return "ok"
        if not clean:
            return "ok"
        if clean.lower() in ("exit", "quit"):
            a.shutdown()
            return "exit"
        if clean.startswith("==="):
            try:
                self.command(clean)
            except Exception as e:
                chat(f"{RED}❌ command error: {e}{RST}")
            return "ok"
        body = clean[1:].strip() if clean[:1] in "+!" else clean
        if not a.busy:
            a.submit_task(body, sel, ctx)
        elif clean.startswith("+") or not a.steerable():
            a.submit_task(body, sel, ctx)
            chat(dim(f"  ⏳ queued request: {body}"))
        else:
            steer = body
            if sel:
                steer += f"\n[SELECTION] {sel.get('file')} lines {sel.get('start')}-{sel.get('end')}:\n{sel.get('text', '')}"
            a.submit_steer(steer, interrupt=clean.startswith("!"))
            if not clean.startswith("!"):
                chat(dim("  ↪ steering queued for the next turn"))
        return "ok"

    def command(self, cmd: str):
        a = self.a
        if cmd == "===h":
            chat(HELP_TEXT)
        elif cmd == "====":
            s, t = a.clear_queues()
            chat(dim(f"cleared {s} steering, {t} queued request(s)"))
        elif cmd == "===r":
            a.history.clear()
            a.trim_told = False
            chat(dim("conversation memory cleared: I start planning afresh (the plan file is untouched)"))
        elif cmd == "===c":
            BRIDGE.clear("chat")
        elif cmd == "===p":
            a.submit_task("review the changes reported on the network", kind="review")
        elif cmd == "===s":
            threading.Thread(target=self._status, daemon=True).start()
        elif cmd == "===d":
            threading.Thread(target=self._diff, daemon=True).start()
        elif cmd.startswith("===n"):
            self._net(cmd[4:])
        else:
            chat(dim(f"unknown command {cmd} — ===h for help"))

    def _status(self):
        st = self.a.plan_state(cancellable=False)
        if not st.get("open"):
            plans = st.get("plans") or []
            chat(dim("no plan is open; plans here: " + (", ".join(p["name"] for p in plans) or "none")))
            return
        chat(f"{BOLD}{st.get('summary')}{RST}")
        chat(f"  in progress: {', '.join(st.get('wip') or []) or 'nothing'}")
        chat(f"  ready to start: {', '.join(st.get('ready') or []) or 'nothing'}")
        for w in (st.get("warnings") or [])[:12]:
            chat(f"{YELLOW}  ⚠ {w}{RST}")
        if self.a.deferred:
            chat(f"{YELLOW}  ⏸ {len(self.a.deferred)} reported change(s) wait for review: ===p{RST}")

    def _diff(self):
        try:
            chat(dim(str(self.a.plan("show_diff", cancellable=False))))
        except PlanError as e:
            chat(f"{RED}{e}{RST}")

    def _net(self, rest: str):
        net = self.a.iface
        sub, _, arg = rest.strip().partition(" ")
        if sub == "j":
            name, _, purpose = arg.strip().partition(" ")
            net.join(name or None, purpose.strip() or None)
        elif sub == "l":
            net.leave()
        elif sub == "s" and arg.strip():
            net.say(arg.strip())
        elif sub == "w":
            chat(dim(net.net.who() if net.connected else "not connected"))
        else:
            s = net.summary()
            chat(dim(f"🌐 {'joined' if s['joined'] else 'not joined'} · {'connected' if s['connected'] else 'disconnected'} "
                     f"· {s['network'] or 'network'} {s['hub']} as @{s['name']} [{s['state']}]\n"
                     "   ===nj [name] [purpose] join · ===nl leave · ===ns <text> say · ===nw who"))


# ==========================================
# 5. BOOTSTRAP
# ==========================================
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mksk: the make-sketch planning agent (backend of mksk.lua).")
    p.add_argument("--dir", default=os.getcwd(), help="folder holding the plan files (default: current directory)")
    p.add_argument("--plan", default="", help="plan to open at start (default: the only plan in --dir)")
    p.add_argument("--port", type=int, default=8080, help="port of the local LLM server")
    p.add_argument("--host", default="localhost", help="hostname of the local LLM server")
    p.add_argument("--max-turns", type=int, default=24, help="actions per request")
    p.add_argument("--max-output", type=int, default=6000, help="chars of one action result fed to the LLM")
    p.add_argument("--max-plan", type=int, default=30000, help="chars of the plan shown to the LLM each turn")
    p.add_argument("--history-chars", type=int, default=120000,
                   help="size of the conversation kept in context; beyond it the oldest action steps are forgotten")
    p.add_argument("--keep-result", type=int, default=700, help="chars kept of an action result once its request is over")
    p.add_argument("--diff-lines", type=int, default=80, help="diff lines shown in the chat (v shows all)")
    p.add_argument("--cmd-timeout", type=int, default=30, help="seconds to wait for Neovim to run a plan function")
    p.add_argument("--ask-timeout", type=int, default=180, help="seconds ask_agent waits for an answer")
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("MKSK_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    return p.parse_args(argv)


def main():
    global ARGS, CLIENT, MODEL_NAME
    if sys.stdin.isatty():
        print("mksk.py is the backend of the Neovim UI. Inside nvim run:\n"
              "  :source ~/echo-home/.vim/mimd/mksk/mksk.lua   (or :Mksk --port 8080)", file=sys.stderr)
    ARGS = parse_args(sys.argv[1:])
    threading.Thread(target=BRIDGE.flusher, daemon=True).start()
    CLIENT = OpenAI(base_url=f"http://{ARGS.host}:{ARGS.port}/v1", api_key="localm", timeout=600)
    MODEL_NAME = get_model_name(ARGS.host, ARGS.port)

    agent = MkskAgent()
    BRIDGE.state_fn = agent.state
    router = Router(agent)

    chat(f"\033[1;36m◆ mksk\033[0m make-sketch planner · \033[1m{MODEL_NAME or 'no LLM'}\033[0m")
    if not MODEL_NAME:
        chat(f"{RED}  cannot reach the LLM at http://{ARGS.host}:{ARGS.port} — start it; I try again on each request{RST}")
    chat(dim("  I stage plan edits and ask before anything is written · ===h help · F8 network"))
    ilog("IFACE — Intelligence Network traffic and decisions (F8 back to chat)", "Title")
    import mksk_iface
    if mksk_iface.INET_ERROR:
        ilog("⚠️ Intelligence Network driver not loaded; mksk plans but cannot join.", "ErrorMsg")
        ilog("   " + mksk_iface.INET_ERROR, "ErrorMsg")
    else:
        ilog(f"   inet driver: {mksk_iface.INET_DIR}", "Comment")
    threading.Thread(target=agent.worker, daemon=True).start()
    if ARGS.net_name:
        agent.iface.join()

    try:
        for line in iter(sys.stdin.readline, ""):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "res":
                BRIDGE.resolve(msg)
                continue
            if t == "input":
                if router.accept(msg.get("text", ""), msg.get("sel"), msg.get("ctx")) == "exit":
                    break
            elif t == "interrupt":
                agent.interrupt()
            elif t == "cancel":
                agent.cancel()
            elif t == "clear" and msg.get("b") in Bridge.BUFS:
                BRIDGE.clear(msg["b"])
            elif t == "net":
                op = msg.get("op")
                if op == "join":
                    agent.iface.join(msg.get("name") or None, msg.get("purpose") or None,
                                     msg.get("hub") or None, msg.get("network") or None)
                elif op == "leave":
                    agent.iface.leave()
                elif op == "say":
                    agent.iface.say(msg.get("text", ""), to=msg.get("to"))
            elif t == "quit":
                break
            BRIDGE.mark_dirty()
    finally:
        agent.shutdown()
        agent.iface.net.close()
        BRIDGE.emit({"t": "exit"})
        BRIDGE.running = False
        try:
            BRIDGE.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
