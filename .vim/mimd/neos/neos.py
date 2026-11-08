#!/usr/bin/env python
"""
Neos: autonomous Neovim operator with Uvian memory. Headless backend of neos.lua (same folder).

Ported from ~/echo-home/workstation/lmstudio/promptia/neos.py (the terminal REPL, left untouched).
The Lua frontend owns the UI (one floating chat window over one persistent buffer) and executes
every editor action inside the user's real window, so this process never touches Neovim directly
(no pynvim needed).

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"style","id":N,"fg":..,"bg":..,"bold":b,"italic":b,"underline":b,"reverse":b}
  {"t":"w","b":"chat"|"log","s":[[text, style id], ...]}     append text
  {"t":"clear","b":"chat"|"log"}
  {"t":"state", busy, phase, model, auto, approval:{kind,detail,editing}|null,
                steer:[..], tasks:[..], loads:[..], uvian:bool}
  {"t":"req","id":N,"op":"context"|"position"|"focus"|"term"|"ex"|"lua"|"read"|"edit", "win":winid?, ...}   run in the target window
  {"t":"input","text":..}      put text in the input line (approval edit mode)
  {"t":"show"}                 open/focus the float (user input needed)
  {"t":"notify","text":..,"level":"info"|"warn"}   shown when the float is hidden
  {"t":"exit"}

nvim -> backend
  {"t":"input","text":..,"sel":{file,start,end,text}|null,"anchor":winid}   anchor = window the user asked from
  {"t":"res","id":N,"ok":b,"out":..,"err":..,"after":{..}}
  {"t":"interrupt"} | {"t":"cancel"} | {"t":"clear","b":..} | {"t":"quit"}
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

# Claim the real stdout for the protocol before anything can print to it.
_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

import requests  # noqa: E402
import yaml  # noqa: E402
from openai import OpenAI  # noqa: E402

ARGS: Optional[argparse.Namespace] = None
CLIENT: Optional[OpenAI] = None
MODEL_NAME: Optional[str] = None

# ==========================================
# 1. ANSI -> NVIM HIGHLIGHT BRIDGE (same scheme as memphia_nvim.py)
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
    BUFS = ("chat", "log")

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

    # ----- requests into nvim (called from the worker thread) -----
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


def chat(text: str = ""):
    BRIDGE.write("chat", str(text) + "\n")


def logw(text: str = ""):
    BRIDGE.write("log", str(text) + "\n")


def dim(s: str) -> str:
    return f"\033[90m{s}\033[0m"


def clip(text: str, limit: int) -> str:
    """Keep head and tail of long text (errors usually live at the end)."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars omitted] ...\n{text[-tail:]}"


def preview(text: str, lines: int = 4, width: int = 160) -> str:
    rows = [r[:width] for r in (text or "").strip("\n").split("\n")]
    more = len(rows) - lines
    rows = rows[:lines]
    if more > 0:
        rows.append(f"… {more} more line(s) in the log (F2)")
    return "\n".join("    " + r for r in rows)


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
    return None


# ==========================================
# 2. UVIAN MEMORY (search via uvian/.function/entry-search)
# ==========================================
def parse_summer_lines(text: str) -> List[Tuple[str, dict]]:
    out = []
    for line in (text or "").splitlines():
        if ": " not in line:
            continue
        uid, _, body = line.partition(": ")
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append((uid.strip(), obj))
    return out


def summary_of(entry: dict, width: int = 200) -> str:
    if "encrypted" in entry:
        return "(encrypted entry)"
    if entry.get("hint"):
        s = entry["hint"]
    else:
        vals = [v for k, v in entry.items() if k not in ("score", "path") and isinstance(v, str)]
        s = min(vals, key=len) if vals else "(no summary)"
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "…"


class Uvian:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.script = os.path.join(self.root, ".function", "entry-search")

    @property
    def available(self) -> bool:
        return os.path.isfile(self.script)

    def path_for(self, uid: str) -> str:
        return os.path.join(self.root, uid[0:1], uid[1:2], uid[2:4], uid)

    def exists(self, uid: str) -> bool:
        return bool(uid) and len(uid) >= 4 and "/" not in uid and os.path.isfile(self.path_for(uid))

    def search(self, query: str, top: int) -> List[dict]:
        proc = subprocess.run(
            [sys.executable, self.script, "-d", self.root, "-n", str(top), "--", query],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip()[-300:] or f"exit {proc.returncode}")
        return [{"uuid": u, "score": e.get("score", 0), "summary": summary_of(e), "entry": e}
                for u, e in parse_summer_lines(proc.stdout)]

    def is_encrypted(self, uid: str) -> bool:
        try:
            with open(os.path.join(self.root, "summer.yaml"), "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith(uid + ":") and '"encrypted"' in line:
                        return True
        except OSError:
            pass
        return False

    def read(self, uid: str, limit: int) -> str:
        if not self.exists(uid):
            raise FileNotFoundError(f"uvian entry {uid} not found")
        if self.is_encrypted(uid):
            raise PermissionError(f"uvian entry {uid} is gpg-encrypted; Neos does not decrypt it")
        with open(self.path_for(uid), "rb") as f:
            raw = f.read(limit * 2)
        if b"\x00" in raw[:2048]:
            raise ValueError(f"uvian entry {uid} is binary")
        return clip(raw.decode("utf-8", errors="ignore"), limit)


def format_hits(hits: List[dict]) -> str:
    return "\n".join(f"- {h['uuid']} (score {h['score']}): {h['summary']}" for h in hits)


# ==========================================
# 2b. USER VIM SETUP DOC (agent_doc.md written by :NeosDoc from ~/.vimrc.neo)
# ==========================================
_DOC_STOP = {"the", "and", "for", "with", "how", "what", "can", "you", "use", "using", "into", "from", "that",
             "this", "please", "show", "tell", "remind", "again", "does", "do", "is", "my", "me", "a", "an", "to",
             "of", "in", "on", "it", "i", "which", "key", "keys", "map", "function", "command", "vim"}


def _doc_tokens(text: str) -> List[str]:
    out = []
    for w in re.findall(r"[a-z0-9_]+", text.lower()):
        if w in _DOC_STOP:
            continue
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


class VimDoc:
    """One searchable unit per "- ..." line, carrying its "## SECTION (file)" heading."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self._mtime: Optional[float] = None
        self.units: List[Tuple[str, str, set, set]] = []  # heading, line, tokens, backticked names

    @property
    def available(self) -> bool:
        return os.path.isfile(self.path)

    def _load(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            self.units, self._mtime = [], None
            return
        if mt == self._mtime:
            return
        units, heading, sub = [], "", ""
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("## "):
                    heading, sub = line[3:].strip(), ""
                elif line.startswith("### "):
                    sub = line[4:].strip()
                elif line.startswith("- "):
                    head = f"{heading} / {sub}" if sub else heading
                    names = {n.lower().lstrip(":") for n in re.findall(r"`+([^`]+?)`+", line)}
                    units.append((head, line, set(_doc_tokens(head + " " + line)), names))
        self.units, self._mtime = units, mt

    def search(self, query: str, top: int = 8) -> List[str]:
        self._load()
        terms = list(dict.fromkeys(_doc_tokens(query)))
        raw = {w.lower().lstrip(":") for w in query.split()}
        if not self.units or not (terms or raw):
            return []
        n = len(self.units)
        df = {t: sum(1 for u in self.units if t in u[2]) for t in terms}
        scored = []
        for head, line, toks, names in self.units:
            score = sum(math.log(1 + n / df[t]) for t in terms if df[t] and t in toks)
            exact = raw & {nm.split()[0] for nm in names if nm}
            if exact:
                score += 5.0 * len(exact)
            if score > 0:
                scored.append((score, f"[{head}] {line[2:]}"))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][0] if scored else 0
        return [s for sc, s in scored[:top] if sc >= 0.5 * best]


# ==========================================
# 2c. SCOPE POLICY: anything native (built-in Ex commands/functions, Neovim Lua API) + ~/.vimrc.neo
#     Only non-native code is rejected: user/plugin commands and functions not defined in ~/.vimrc.neo,
#     require() of plugin modules, and plain-Lua os/io/dofile/load (not part of the Neovim API).
# ==========================================
class VimrcScope:
    """Functions and user commands defined in ~/.vimrc.neo (re-read when the file changes)."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self._mtime: Optional[float] = None
        self.functions: Dict[str, str] = {}  # name -> "Name(args) — note"
        self.commands: Dict[str, str] = {}   # name -> definition

    def load(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            self.functions, self.commands, self._mtime = {}, {}, None
            return
        if mt == self._mtime:
            return
        funcs, cmds, notes = {}, {}, []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                t = raw.strip()
                if t.startswith('"'):
                    c = t.lstrip('"').strip().lstrip(":").strip()
                    if c and not re.match(r"^-{3,}", c):
                        notes.append(c)
                    continue
                m = re.match(r"^fu(?:n|nc|nct|ncti|nctio|nction)?!?\s+([A-Za-z_][\w#:.]*)\s*\((.*?)\)", t)
                if m and not m.group(1).startswith("s:"):
                    note = "; ".join(n for n in notes[-2:] if not re.match(r"^[a-z]+!?\s", n))
                    funcs[m.group(1)] = f"{m.group(1)}({m.group(2)})" + (f" — {note}" if note else "")
                m = re.match(r"^com(?:m|ma|man|mand)?!?\s+(.*)$", t)
                if m:
                    rest = m.group(1)
                    while rest.startswith("-"):
                        rest = rest.split(None, 1)[1] if " " in rest else ""
                    parts = rest.split(None, 1)
                    if parts:
                        cmds[parts[0]] = parts[1] if len(parts) > 1 else ""
                if t:
                    notes = []
        self.functions, self.commands, self._mtime = funcs, cmds, mt

    def describe(self) -> str:
        self.load()
        rows = [f"- function {v}" for v in self.functions.values()]
        rows += [f"- command :{k} → {v}" for k, v in self.commands.items()]
        return "\n".join(rows) or "(none found)"


class Policy:
    """Lets every native Neovim command/function/API through; rejects non-native (plugin/user) code."""

    DO_CMDS = {"windo", "bufdo", "tabdo", "argdo", "cdo", "cfdo", "ldo", "lfdo", "folddoopen", "folddoclosed",
               "silent", "sil", "keepjumps", "keepj", "keepmarks", "keepp", "keeppatterns", "lockmarks",
               "noautocmd", "noa", "unsilent", "verbose", "verb", "vertical", "vert", "aboveleft", "abo",
               "belowright", "bel", "topleft", "to", "botright", "bo", "tab", "hide", "confirm", "browse",
               "leftabove", "lefta", "rightbelow", "rightb", "sandbox", "legacy", "horizontal", "hor"}
    RANGE = re.compile(r"^(?:\s|:|[%.$\d,;+\-*]|'[a-zA-Z<>\[\]\"^.`]|/(?:\\.|[^/])*/?|\?(?:\\.|[^?])*\??)*")

    def __init__(self, scope: VimrcScope):
        self.scope = scope

    # ----- function calls inside Vimscript expressions -----
    def _check_calls(self, text: str) -> Optional[str]:
        for m in re.finditer(r"(?<![\w#:.'\"])([A-Za-z_][\w#:.]*)\s*\(", text):
            name = m.group(1)
            if name[0].isupper() or "#" in name or name.startswith(("g:", "s:")):
                if name.removeprefix("g:") not in self.scope.functions:
                    return f"function {name}() is not native and not defined in {self.scope.path}"
        return None

    def _split_bar(self, line: str) -> List[str]:
        parts, cur, i = [], "", 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line):
                cur += line[i:i + 2]
                i += 2
                continue
            if ch == "|":
                parts.append(cur)
                cur = ""
            else:
                cur += ch
            i += 1
        parts.append(cur)
        return parts

    def check_ex(self, line: str, depth: int = 0) -> Optional[str]:
        self.scope.load()
        if depth > 6:
            return "command nesting too deep"
        for part in self._split_bar(line):
            err = self._check_one(part, depth)
            if err:
                return err
        return None

    def _check_one(self, cmd: str, depth: int) -> Optional[str]:
        cmd = cmd.strip()
        if not cmd or cmd.startswith('"'):
            return None
        rng = self.RANGE.match(cmd).group(0)
        rest = cmd[len(rng):]
        if rest.startswith("!"):
            return None  # :!cmd / :{range}!filter are native
        m = re.match(r"([A-Za-z]\w*|[&~<>=@#])(!?)\s*(.*)$", rest, re.DOTALL)
        if not m:
            return None
        name, bang, arg = m.group(1), m.group(2), m.group(3)
        low = name.lower()
        if name[0].isupper():
            if name not in self.scope.commands:
                return f"command :{name} is not native (plugin/user) and not defined in {self.scope.path}"
            return self._check_calls(arg)
        if low in ("lua",):
            return self.check_lua(arg[1:] if arg.startswith("=") else arg)
        if low in ("g", "gl", "glo", "glob", "globa", "global", "v", "vg", "vgl", "vglobal") and arg:
            sep = arg[0]
            body = arg[1:]
            idx = 0
            while idx < len(body):
                if body[idx] == "\\":
                    idx += 2
                    continue
                if body[idx] == sep:
                    break
                idx += 1
            return self.check_ex(body[idx + 1:] or "p", depth + 1)
        if low in self.DO_CMDS:
            return self.check_ex(arg, depth + 1) if arg.strip() else None
        if low in ("cal", "call", "let", "echo", "ec", "echom", "echomsg", "echon", "if", "elseif", "while",
                   "for", "return", "unl", "unlet", "echoerr", "echoe", "const", "cons"):
            return self._check_calls(arg)
        if low in ("exe", "exec", "execute"):  # native; check what it builds when it is a plain string
            lits = re.findall(r"'((?:''|[^'])*)'|\"((?:\\.|[^\"\\])*)\"", arg)
            for a, b in lits:
                err = self.check_ex(a or b, depth + 1) if (a or b).strip() else None
                if err:
                    return err
            return self._check_calls(arg)
        if low in ("s", "su", "sub", "substitute", "sno", "snomagic", "sm", "smagic", "&&", "~") and "\\=" in arg:
            return self._check_calls(arg)
        return None

    LUA_BAD = [
        (r"\bos\s*\.", "os.* (plain Lua stdlib, not the Neovim API — use vim.fn / vim.system / vim.uv)"),
        (r"\bio\s*\.", "io.* (plain Lua stdlib, not the Neovim API — use vim.fn.readfile / writefile)"),
        (r"\b(loadstring|loadfile|dofile)\s*\(|(?<![\w.])load\s*\(", "dynamic code loading"),
        (r"\brequire\s*\(?\s*[\"'](?!vim[.\"'])", "require of non-vim modules (plugins)"),
        (r"\bpackage\s*\.", "package.* (module loading)"),
        (r"\bdebug\s*\.", "debug.*"),
    ]

    def check_lua(self, code: str) -> Optional[str]:
        self.scope.load()
        for pat, why in self.LUA_BAD:
            if re.search(pat, code):
                return f"Lua uses {why}"
        for m in re.finditer(r"vim\s*\.\s*fn\s*(?:\.\s*([A-Za-z_][\w#]*)|\[\s*[\"']([^\"']+)[\"']\s*\])", code):
            name = m.group(1) or m.group(2)
            if (name[0].isupper() or "#" in name) and name not in self.scope.functions:
                return f"vim.fn.{name} is not native and not defined in {self.scope.path}"
        for m in re.finditer(r"vim\s*\.\s*call\s*\(\s*[\"']([^\"']+)[\"']|nvim_call_function\s*\(\s*[\"']([^\"']+)[\"']", code):
            name = m.group(1) or m.group(2)
            if (name[0].isupper() or "#" in name) and name not in self.scope.functions:
                return f"{name}() is not native and not defined in {self.scope.path}"
        for m in re.finditer(r"vim\s*\.\s*cmd\s*\.\s*([A-Za-z_]\w*)", code):
            err = self.check_ex(m.group(1))
            if err:
                return err
        lit = r"(?:\"((?:\\.|[^\"\\])*)\"|'((?:\\.|[^'\\])*)'|\[(=*)\[(.*?)\]\3\])"
        for m in re.finditer(r"(?:vim\s*\.\s*cmd|nvim_command|nvim_exec2?|nvim_cmd)\s*\(?\s*" + lit, code, re.DOTALL):
            ex = m.group(1) or m.group(2) or m.group(4) or ""
            for line in ex.split("\n"):
                err = self.check_ex(line)
                if err:
                    return err
        return None


# ==========================================
# 3. AGENT
# ==========================================
SYSTEM_PROMPT = """You are Neos, an autonomous Neovim operator. You have direct access to the user's running Neovim.
Your goal is to fulfill the User Intent inside their current editor session. You answer in a floating chat window.

SCOPE (enforced before anything runs) — you are free to use anything native to Neovim:
- every built-in Ex command (:terminal, :!cmd, :{range}!filter, :make, :normal, :execute, ... included),
- every built-in function (system(), jobstart(), getline(), ...) and the Neovim Lua API (vim.api, vim.fn, vim.cmd,
  vim.system, vim.uv, vim.o/bo/wo, vim.keymap, ...),
- the functions and commands listed in [VIMRC] (defined in the user's ~/.vimrc.neo).
Rejected: plugin or other user commands/functions not in [VIMRC], require() of plugin modules, plain-Lua os./io./dofile/load.

RULES:
1. You operate in a loop: each turn run ONE action, then read its result.
2. [TASK ANCHOR] is where the user was when they made this request: tab, window (winid), buffer, file, cursor line/col and
   that line's text. "here", "this line", "this file", "this function", "the current window" refer to it, for the whole
   task, even if the user switches windows meanwhile (where they went does not matter for this task).
   Ex/Lua/read/edit actions run in the anchor window unless you pass "win"; "focus" moves the user AND the anchor.
   These blocks are a live placeholder re-read before each of your turns (always the last message).
   [EDITOR] (from the start of the request) adds the text around the cursor.
2b. [NAV MAP] (live, re-read before each turn) lists every tab and window with its winid; terminals show whether the
   job runs and their last line. To act in another window pass "win"; to move the user there use "focus"; to run a
   shell command in a terminal use "terminal_send" (its latest output comes back; read_lines with that "win" shows
   more). ALWAYS reuse an existing TERMINAL window from [NAV MAP] whose job is running; open a new one
   (run_command "vsplit | terminal") only when none exists or the user asks for a new one, then terminal_send to
   its winid on the next turn.
3. Line numbers are 1-based; lines are shown as "<n>| text" (">" marks the cursor line).
4. Prefer "edit_lines" for changing text, and read the result back before declaring SUCCESS.
5. Avoid commands that block waiting for input inside your call (confirm flags, input()); never quit Neovim.
6. If the intent is a question, answer it right away with "declare_result".
   Shell work goes through native means (":!cmd" output comes back to you; ":terminal" opens an interactive one).
   Do not confuse look-alikes (":ls" is Vim's buffer list, not the shell). If the intent needs a plugin that is not in
   [VIMRC], declare_result FAILED and say so. In "reply", claim only what the action results actually show.
7. When the user asks how to do something, prefer THEIR [VIMRC] function/command or [USER VIM SETUP] mapping
   (exact keys) over stock advice; "vim_doc" searches their setup further.
8. [SUCCESSFUL EARLIER REQUESTS] shows what already worked in this session; reuse those approaches.
9. Messages starting with "User Steering Update" come from the user mid-task and override earlier plans.
10. Output ONLY one valid JSON object with only the fields your action needs. No markdown outside "reply".

OUTPUT SCHEMA:
{
  "action_type": "run_command" | "run_lua" | "read_lines" | "edit_lines" | "focus" | "terminal_send" | "vim_doc" | "declare_result",
  "win": <winid from [NAV MAP]: run_command/run_lua/read_lines/edit_lines act in that window instead of the user's;
          required for terminal_send; for focus it is the window to move the user to>,
  "tab": <tab number, for focus>,
  "keys": "<text typed into the terminal, for terminal_send; Enter is added unless \"enter\": false>",
  "wait": <ms to wait for terminal output, default 1500, max 15000>,
  "command": "<Ex command, for run_command>",
  "code": "<Lua chunk using the Neovim API, for run_lua; return values and print() output come back>",
  "start": <first line>, "end": <last line>, "buffer": <bufnr or null>,   (read_lines / edit_lines)
  "lines": ["<new text>", ...],   (edit_lines: replaces start..end; end = start-1 inserts; [] deletes)
  "query": "<keywords, for vim_doc>",
  "status": "SUCCESS" or "FAILED",   (declare_result)
  "reply": "<declare_result: the concrete answer or result for the user — name the files, tabs, lines, keys>",
  "reason": "<one short sentence: why this step>"
}
"""

HELP_TEXT = """\033[1mNeos commands\033[0m
  <text>          idle: new task │ busy: steering message for the next turn
  !<text>         steer and interrupt the current LLM generation now
  +<text>         queue a task to run after the current one
  ===u <keywords> search Uvian memory and load the top hits into the next question (vim: neosu / :NeosUvian)
  =-= <keywords>  search Uvian only (Enter on a #n line loads that entry)
  ===a            toggle auto-approve for editor actions
  ===x            cancel the running task      ====  clear queues
  ===r            forget the remembered successful requests
  ===c            clear the chat               ===h  this help
  :NeosDoc        (vim) rebuild the vim setup doc Neos answers "how do I…" from
\033[1mKeys\033[0m (in the float)  C-x interrupt │ C-c cancel/clear │ F2 log view │ M-h/j/k/l dock, M-c center, M-f full, M-b peek │ q/Esc hide │ ↑/↓ history
  approvals: y yes │ n no │ a yes + always │ e edit the command/code
  scope: anything native (built-in Ex commands/functions, Neovim Lua API) + ~/.vimrc.neo functions/commands;
         plugin/user code outside ~/.vimrc.neo is rejected
  memory: only requests that ended in SUCCESS are remembered for later questions"""

ALIASES = {
    "run_command": "ex", "command": "ex", "ex_command": "ex", "ex": "ex", "vim": "ex",
    "run_lua": "lua", "lua": "lua",
    "read_lines": "read", "get_lines": "read", "read": "read",
    "edit_lines": "edit", "set_lines": "edit", "replace_lines": "edit", "edit": "edit",
    "focus": "focus", "switch_window": "focus", "goto_window": "focus", "focus_window": "focus",
    "terminal_send": "term", "send_terminal": "term", "term_send": "term", "terminal": "term",
    "vim_doc": "vim_doc", "declare_result": "done", "finish": "done", "answer": "done", "done": "done",
}

class Cancelled(Exception):
    pass


def autopilot_allows() -> bool:
    try:
        with open(os.path.expanduser("~/echo-home/autopilot.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = (cfg.get("neos") or {}).get("allow", False)
        return v is True or str(v).strip().lower() in ("yes", "true", "1", "y")
    except Exception:
        return False


class NeosAgent:
    def __init__(self, uvian: Optional[Uvian], vimdoc: Optional[VimDoc] = None, policy: Optional[Policy] = None):
        self.uvian, self.vimdoc, self.policy = uvian, vimdoc, policy
        self.done_actions: List[str] = []  # actions that succeeded in the current task
        self.anchor: Optional[int] = None   # winid the current task is pinned to
        self.lock = threading.RLock()
        self.tasks: deque = deque()
        self.steering: deque = deque()
        self.attachments: deque = deque()
        self.wake = threading.Event()
        self.cancel_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.running = True
        self.busy = False
        self.phase = "idle"
        self.auto = bool(ARGS.auto)
        self.approval: Optional[dict] = None
        self.last_search: List[dict] = []
        self.recent: deque = deque(maxlen=8)  # only requests that ended in SUCCESS

    # ---------- UI thread API ----------
    def state(self) -> dict:
        with self.lock:
            ap = self.approval
            return {
                "t": "state", "busy": self.busy, "phase": self.phase, "model": MODEL_NAME, "auto": self.auto,
                "approval": {"kind": ap["kind"], "detail": ap["detail"], "editing": ap["editing"]} if ap else None,
                "steer": list(self.steering), "tasks": [t["text"] for t in self.tasks],
                "loads": list(self.attachments),
                "uvian": bool(self.uvian and self.uvian.available),
            }

    def submit_task(self, text: str, sel: Optional[dict] = None, anchor: Optional[int] = None):
        with self.lock:
            self.tasks.append({"text": text, "sel": sel, "anchor": anchor})
        self.wake.set()
        BRIDGE.mark_dirty()

    def submit_steer(self, text: str, interrupt: bool = False):
        with self.lock:
            self.steering.append(text)
        if interrupt:
            self.interrupt_event.set()
        BRIDGE.mark_dirty()

    def attach(self, uid: str):
        with self.lock:
            if uid not in self.attachments:
                self.attachments.append(uid)
        BRIDGE.mark_dirty()

    def clear_queues(self) -> Tuple[int, int, int]:
        with self.lock:
            n = (len(self.steering), len(self.tasks), len(self.attachments))
            self.steering.clear()
            self.tasks.clear()
            self.attachments.clear()
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
            chat("\033[33m⏹ interrupt requested\033[0m")

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

    # ---------- waiting on the user ----------
    def _wait(self, req: dict):
        try:
            while not req["event"].wait(0.1):
                if self.cancel_event.is_set() or not self.running:
                    break
        finally:
            with self.lock:
                self.approval = None
            BRIDGE.mark_dirty()

    def request_approval(self, kind: str, detail: str, editable: bool) -> Tuple[bool, str]:
        if self.auto or autopilot_allows():
            return True, detail
        req = {"kind": kind, "detail": detail, "editable": editable, "editing": False,
               "event": threading.Event(), "answer": None}
        with self.lock:
            self.approval = req
        BRIDGE.emit({"t": "show"})
        shown = detail if len(detail) < 600 else detail[:600] + "…"
        chat(f"\033[31m⚠ allow {kind}?\033[0m\n\033[1m{shown}\033[0m")
        chat(dim("  y yes │ n no │ a yes + always │ e edit" if editable else "  y yes │ n no │ a yes + always"))
        self.set_phase("waiting approval")
        self._wait(req)
        return req["answer"] or (False, detail)

    def answer_approval(self, text: str) -> bool:
        """Returns True when the line should be refilled (edit mode)."""
        with self.lock:
            req = self.approval
        if not req:
            return False
        t = text.strip()
        if req["editing"]:
            req["answer"] = (bool(t), t or req["detail"])
            req["event"].set()
            return False
        low = t.lower()
        if low in ("y", "yes"):
            req["answer"] = (True, req["detail"])
        elif low in ("n", "no"):
            req["answer"] = (False, req["detail"])
        elif low in ("a", "always"):
            self.auto = True
            chat(dim("  auto-approve ON for this session (===a to turn off)"))
            req["answer"] = (True, req["detail"])
        elif low == "e" and req["editable"]:
            req["editing"] = True
            BRIDGE.mark_dirty()
            return True
        else:
            chat(dim("  pending approval: answer y, n, a or e — other text is queued as steering"))
            self.submit_steer(t)
            return False
        req["event"].set()
        return False

    # ---------- worker ----------
    def worker(self):
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
            self.busy = True
            BRIDGE.mark_dirty()
            try:
                self.run_task(task)
            except Cancelled:
                chat("\033[33m🛑 task cancelled\033[0m")
            except Exception as e:
                chat(f"\033[31m❌ {type(e).__name__}: {e}\033[0m")
                logw(f"[AGENT ERROR] {type(e).__name__}: {e}")
            finally:
                self.busy = False
                self.anchor = None
                self.set_phase("idle")

    # ---------- editor RPC ----------
    def nvim(self, op: str, **params) -> dict:
        if op in ("ex", "lua", "read", "edit") and params.get("win") is None and self.anchor:
            params["win"] = self.anchor  # the task stays on the window it started in
        logw(f"\033[36m→ {op}\033[0m {json.dumps(params, ensure_ascii=False)[:2000]}")
        res = BRIDGE.request(op, ARGS.cmd_timeout, self.cancel_event, **params)
        body = res.get("out") if res.get("ok") else res.get("err")
        logw(f"\033[36m← {op}\033[0m ok={res.get('ok')} {str(body or '')[:4000]}")
        return res

    # ---------- context ----------
    @staticmethod
    def format_position(pos: Optional[dict], label: str = "USER POSITION") -> str:
        if not pos:
            return f"[{label}] unavailable"
        flags = ", ".join(x for x in (pos.get("filetype") or "", f"buftype {pos['buftype']}" if pos.get("buftype") else "",
                                       "MODIFIED" if pos.get("modified") else "") if x)
        out = (f"[{label}] tab {pos.get('tab')}/{pos.get('tabs')} · window {pos.get('winnr')}/{pos.get('wins')} "
               f"(winid {pos.get('win')}) · buffer {pos.get('buf')} {pos.get('file') or '[No Name]'}"
               f"{' (' + flags + ')' if flags else ''} · cursor line {pos.get('line')}, col {pos.get('col')} "
               f"of {pos.get('line_count')} lines\n  {pos.get('line')}> {pos.get('text', '')}")
        if pos.get("alt"):
            out += f"\n  alternate file (#): {pos['alt']}"
        return out

    @staticmethod
    def format_nav(nav: Optional[list]) -> str:
        if not nav:
            return ""
        rows = ['[NAV MAP] live; act in a window with "win": <winid>, move the user with "focus", type into a terminal '
                'with "terminal_send"']
        for t in nav:
            rows.append(f"tab {t['nr']}{' (current)' if t.get('current') else ''}:")
            for w in t.get("wins", []):
                mark = " ◀ task" if w.get("anchor") else ""
                if w.get("term"):
                    tm = w["term"]
                    state = "running" if tm.get("alive") else "exited"
                    title = f" '{tm['title']}'" if tm.get("title") else ""
                    rows.append(f"  win {w['win']} TERMINAL{title} job {state}{mark}  last line: {tm.get('last', '')!r}")
                else:
                    rows.append(f"  win {w['win']} {w.get('name') or '[No Name]'}{'+' if w.get('modified') else ''}"
                                f" line {w.get('line')}/{w.get('line_count')}"
                                f"{' (' + w['filetype'] + ')' if w.get('filetype') else ''} {w.get('width')}x{w.get('height')}{mark}")
        return "\n".join(rows)

    @classmethod
    def format_context(cls, ctx: Optional[dict], sel: Optional[dict]) -> str:
        if not ctx:
            return "[EDITOR] unavailable"
        cur = ctx.get("cursor") or [0, 0]
        vis = ctx.get("visible") or ["?", "?"]
        lines = [
            "[EDITOR]",
            f"- file: {ctx.get('file') or '[No Name]'}  (buffer {ctx.get('buf')}, filetype '{ctx.get('filetype') or ''}'"
            f"{', buftype ' + ctx['buftype'] if ctx.get('buftype') else ''}{', MODIFIED' if ctx.get('modified') else ''})",
            f"- cursor: line {cur[0]}, col {cur[1] + 1} of {ctx.get('line_count')} lines; visible lines {vis[0]}-{vis[1]}",
            f"- cwd: {ctx.get('cwd')}",
        ]
        if ctx.get("buffers"):
            lines.append("- open buffers: " + ", ".join(
                f"{b['nr']}:{b['name'] or '[No Name]'}{'+' if b.get('modified') else ''}" for b in ctx["buffers"]))
        if ctx.get("capabilities"):
            lines.append("- :AgentCapabilities is available")
        if ctx.get("text"):
            lines.append(f"- text around the cursor:\n{ctx['text']}")
        if sel:
            lines.append(f"\n[SELECTION] {sel.get('file') or '[No Name]'} lines {sel.get('start')}-{sel.get('end')}"
                         f" (the user's request refers to this):\n{sel.get('text', '')}")
        return "\n".join(lines)

    def recent_block(self) -> str:
        if not self.recent:
            return ""
        rows = []
        for r in self.recent:
            rows.append(f"- request: {r['intent'][:200]}\n  result: {r['reply'][:300]}")
            if r["actions"]:
                rows.append("  worked: " + " ; ".join(r["actions"])[:600])
        return "\n\n[SUCCESSFUL EARLIER REQUESTS] (oldest first):\n" + "\n".join(rows)

    def scope_block(self) -> str:
        if not self.policy:
            return ""
        return f"\n\n[VIMRC] functions and commands from {self.policy.scope.path} you may call:\n{self.policy.scope.describe()}"

    def vimdoc_block(self, intent: str) -> str:
        if not (self.vimdoc and self.vimdoc.available) or ARGS.doc_top <= 0:
            return ""
        rows = self.vimdoc.search(intent, ARGS.doc_top)
        if not rows:
            return ""
        chat(dim(f"  📘 vim setup: {len(rows)} matching entr{'y' if len(rows) == 1 else 'ies'}"))
        logw("[USER VIM SETUP]\n" + "\n".join(rows))
        return ("\n\n[USER VIM SETUP] the user's own keymaps/commands/functions matched to the request "
                f"(full doc: {self.vimdoc.path}):\n" + "\n".join(f"- {r[:220]}" for r in rows))

    def load_uvian(self, uid: str) -> str:
        content = self.uvian.read(uid, ARGS.max_output)
        logw(f"[UVIAN {uid}]\n{content}")
        return f"[UVIAN ENTRY {uid}] ({self.uvian.path_for(uid)})\n{content}"

    def apply_attachments(self, messages: List[dict]):
        with self.lock:
            uids = list(self.attachments)
            self.attachments.clear()
        if not uids:
            return
        BRIDGE.mark_dirty()
        for uid in uids:
            try:
                messages.append({"role": "user", "content": "The user loaded this Uvian memory entry for you (reference):\n" + self.load_uvian(uid)})
                chat(dim(f"  📎 loaded uvian {uid}"))
            except Exception as e:
                chat(f"\033[31m  📎 {e}\033[0m")

    # ---------- LLM ----------
    def stream_llm(self, messages: List[dict]) -> Tuple[str, bool]:
        self.set_phase("thinking")
        response = CLIENT.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=0.0, stream=True)
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
                        BRIDGE.write("log", "\033[90m")
                        in_reasoning = True
                    BRIDGE.write("log", reasoning.replace("\n", "\n\033[90m"))
                if delta.content:
                    if in_reasoning:
                        BRIDGE.write("log", "\033[0m\n")
                        in_reasoning = False
                    output += delta.content
                    BRIDGE.write("log", delta.content)
        finally:
            BRIDGE.write("log", "\033[0m\n")
            if interrupted:
                try:
                    response.close()
                except Exception:
                    pass
        return output, interrupted

    # ---------- actions ----------
    def do_action(self, act: dict) -> str:
        """Run one non-final action and return the evidence message for the LLM."""
        kind = act["action"]
        if kind == "ex":
            cmd = str(act.get("command") or "").strip().lstrip(":")
            if not cmd:
                return "System: 'run_command' needs a non-empty \"command\"."
            if err := self.reject(cmd, self.policy.check_ex(cmd) if self.policy else None):
                return err
            ok, cmd = self.request_approval("ex command", cmd, editable=True)
            if ok and (err := self.reject(cmd, self.policy.check_ex(cmd) if self.policy else None)):
                return err
            chat(f"  \033[38;5;112m⚙ :{cmd}\033[0m" + ("" if ok else dim("  (denied)")))
            if not ok:
                return f"The user DENIED the Ex command: {cmd}"
            self.set_phase("running ex")
            return self._evidence(f"Ex command :{cmd}", self.nvim("ex", command=cmd, win=act.get("win")), record=f":{cmd}")
        if kind == "lua":
            code = str(act.get("code") or "")
            if not code.strip():
                return "System: 'run_lua' needs non-empty \"code\"."
            if err := self.reject(code, self.policy.check_lua(code) if self.policy else None):
                return err
            ok, code = self.request_approval("lua", code, editable=True)
            if ok and (err := self.reject(code, self.policy.check_lua(code) if self.policy else None)):
                return err
            chat(f"  \033[38;5;112m⚙ lua\033[0m {code.strip().splitlines()[0][:100]}" + ("" if ok else dim("  (denied)")))
            if not ok:
                return "The user DENIED running this Lua code."
            self.set_phase("running lua")
            return self._evidence("Lua chunk", self.nvim("lua", code=code, win=act.get("win")), record="lua: " + " ".join(code.split())[:160])
        if kind == "focus":
            if act.get("win") is None and act.get("tab") is None:
                return "System: 'focus' needs \"win\" (winid from [NAV MAP]) or \"tab\" (number)."
            self.set_phase("moving")
            res = self.nvim("focus", win=act.get("win"), tab=act.get("tab"))
            if res.get("ok"):  # Neos moved the user on purpose: the task follows
                pr = BRIDGE.request("position", 5, self.cancel_event)
                if pr.get("ok"):
                    self.anchor = (pr["out"].get("position") or {}).get("win")
            chat(f"  \033[38;5;75m➜ focus {'win ' + str(act['win']) if act.get('win') is not None else 'tab ' + str(act.get('tab'))}\033[0m")
            return self._evidence("focus", res, show=False, record=f"focus {act.get('win') or 'tab ' + str(act.get('tab'))}")
        if kind == "term":
            keys = str(act.get("keys") or act.get("command") or "")
            if act.get("win") is None:
                return "System: 'terminal_send' needs \"win\": the winid of a TERMINAL window from [NAV MAP]."
            if not keys.strip():
                return "System: 'terminal_send' needs non-empty \"keys\"."
            ok, keys = self.request_approval(f"terminal input (win {act['win']})", keys, editable=True)
            chat(f"  \033[38;5;112m⌨ term {act['win']}:\033[0m {keys.strip()[:120]}" + ("" if ok else dim("  (denied)")))
            if not ok:
                return f"The user DENIED sending to the terminal: {keys}"
            self.set_phase("terminal")
            res = self.nvim("term", win=act["win"], keys=keys, wait=act.get("wait"), enter=act.get("enter", True))
            return self._evidence(f"terminal_send to win {act['win']}", res, record=f"terminal_send win {act['win']}: {keys.strip()[:120]}")
        if kind == "read":
            self.set_phase("reading")
            res = self.nvim("read", start=act.get("start"), end=act.get("end"), buffer=act.get("buffer"), win=act.get("win"))
            chat(dim(f"  👁 read {res.get('range') or ''} {res.get('name') or ''}".rstrip()))
            return self._evidence("read", res, show=False)
        if kind == "edit":
            lines = act.get("lines")
            if isinstance(lines, str):
                lines = lines.split("\n")
            if not isinstance(lines, list):
                return "System: 'edit' needs \"lines\" as a list of strings."
            lines = [str(l) for l in lines]
            try:
                start, end = int(act.get("start")), int(act.get("end"))
            except (TypeError, ValueError):
                return "System: 'edit' needs integer \"start\" and \"end\"."
            desc = f"lines {start}-{end} → {len(lines)} line(s)"
            body = "\n".join(lines[:12]) + (f"\n… (+{len(lines) - 12})" if len(lines) > 12 else "")
            ok, _ = self.request_approval("edit", f"{desc}\n{body}", editable=False)
            chat(f"  \033[38;5;214m✎ edit {desc}\033[0m" + ("" if ok else dim("  (denied)")))
            if not ok:
                return f"The user DENIED the edit of {desc}."
            self.set_phase("editing")
            res = self.nvim("edit", start=start, end=end, lines=lines, buffer=act.get("buffer"), win=act.get("win"))
            return self._evidence(f"edit {desc}", res, record=f"edit_lines {desc}")
        if kind == "vim_doc":
            q = str(act.get("query") or "").strip()
            if not (self.vimdoc and self.vimdoc.available):
                return "System: the vim setup doc is not available (run :NeosDoc)."
            rows = self.vimdoc.search(q, 15) if q else []
            chat(dim(f"  📘 vim setup '{q}' → {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}"))
            return f"User vim setup entries for '{q}':\n" + ("\n".join(rows) or "(no matches)")
        return (f"System: unknown action_type '{kind}'. Use run_command, run_lua, read_lines, edit_lines, "
                f"focus, terminal_send, vim_doc or declare_result.")

    def reject(self, what: str, err: Optional[str]) -> Optional[str]:
        if not err:
            return None
        first = what.strip().splitlines()[0][:100] if what.strip() else what
        chat(f"  \033[33m⛔ out of scope:\033[0m {first}\n" + dim(f"     {err}"))
        logw(f"[POLICY] rejected: {what}\n  reason: {err}")
        return (f"System: REJECTED by scope policy — {err}. Use built-in Ex commands, the Neovim Lua API "
                f"or the [VIMRC] functions/commands instead.")

    def _evidence(self, what: str, res: dict, show: bool = True, record: str = "") -> str:
        if res.get("ok"):
            if record:
                self.done_actions.append(record)
            out = str(res.get("out") or "")
            if show and out.strip():
                chat(dim(preview(out)))
            return f"{what} OK.\nOutput:\n{clip(out, ARGS.max_output) or '(no output)'}"
        err = str(res.get("err") or "unknown error")
        chat(f"\033[31m    ✗ {err.strip().splitlines()[0][:200] if err.strip() else err}\033[0m")
        return f"{what} FAILED.\nError: {clip(err, ARGS.max_output)}"

    # ---------- the loop ----------
    def run_task(self, task: dict):
        intent, sel = task["text"], task.get("sel")
        logw(f"\n\033[1;36m▶ TASK:\033[0m {intent}")
        self.set_phase("reading editor")
        ctx_res = self.nvim("context", win=task.get("anchor"))  # the window the user asked from
        if not ctx_res.get("ok") and task.get("anchor"):        # it was closed meanwhile
            ctx_res = self.nvim("context")
        self._check_cancel()
        ctx = ctx_res.get("out") if ctx_res.get("ok") else None
        context = self.format_context(ctx, sel)
        live_pos, live_nav = (ctx or {}).get("position"), (ctx or {}).get("nav")
        self.anchor = (live_pos or {}).get("win")
        context += self.scope_block()
        context += self.vimdoc_block(intent)
        context += self.recent_block()
        self.done_actions = []

        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"User Request: {intent}\n\n{context}"}]
        status, reply = "FAILED", "max turns reached"
        for turn in range(ARGS.max_turns):
            self._check_cancel()
            logw(f"\033[36m── turn {turn + 1} ──\033[0m")
            with self.lock:
                steers = list(self.steering)
                self.steering.clear()
            for s in steers:
                chat(dim(f"  ↪ steering: {s.splitlines()[0]}"))
                messages.append({"role": "user", "content": f"User Steering Update: {s}"})
            self.apply_attachments(messages)
            if turn > 0:  # live placeholder: re-read now, never stored in the history
                pr = BRIDGE.request("position", 5, self.cancel_event, win=self.anchor)
                if not pr.get("ok") and self.anchor:  # anchor window was closed: re-anchor where the user is
                    pr = BRIDGE.request("position", 5, self.cancel_event)
                    if pr.get("ok"):
                        messages.append({"role": "user", "content": f"System: the task's window {self.anchor} was closed; "
                                         "the task is now anchored to the user's current window."})
                        chat(dim(f"  ⚓ window {self.anchor} closed — re-anchored"))
                        self.anchor = (pr["out"].get("position") or {}).get("win")
                if pr.get("ok") and isinstance(pr.get("out"), dict):
                    live_pos, live_nav = pr["out"].get("position"), pr["out"].get("nav")

            self.interrupt_event.clear()

            where = self.format_position(live_pos, "TASK ANCHOR")
            live = {"role": "user", "content": "System (live editor state, not a new request):\n" + where
                    + "\n\n" + self.format_nav(live_nav) + f"\n\nThe request you are working on: {intent}"}
            if os.environ.get("NEOS_DEBUG_PROMPT"):  # opt-in: overwrite a file with the exact prompt of this turn
                try:
                    with open(os.environ["NEOS_DEBUG_PROMPT"], "w", encoding="utf-8") as f:
                        for msg in messages + [live]:
                            f.write(f"===== {msg['role']}\n{msg['content']}\n")
                except OSError:
                    pass
            raw, interrupted = self.stream_llm(messages + [live])
            self._check_cancel()
            if interrupted:
                chat(dim("  ⏹ generation interrupted"))
                messages.append({"role": "user", "content": "System: The user interrupted this generation turn."})
                continue
            obj = parse_llm_json(raw)
            if obj is None:
                chat(dim("  ⚠ reply was not JSON; retrying"))
                messages.append({"role": "assistant", "content": raw[-2000:]})
                messages.append({"role": "user", "content": "System: Reply with ONLY one JSON object per the schema."})
                continue
            kind = str(obj.get("action_type") or obj.get("action") or "").strip().lower()
            obj["action"] = ALIASES.get(kind, kind)
            messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})
            if obj.get("reason") and obj["action"] != "done":
                chat(f"  \033[3;90m💭 {obj['reason']}\033[0m")

            if obj["action"] == "done":
                status = str(obj.get("status") or "SUCCESS").upper()
                reply = str(obj.get("reply") or obj.get("reason") or "")
                colour = "\033[1;32m" if status == "SUCCESS" else "\033[1;31m"
                chat(f"{colour}◆\033[0m {reply}")
                break
            evidence = self.do_action(obj)
            self._check_cancel()
            messages.append({"role": "user", "content": evidence})
        else:
            chat("\033[33m⚠ max turns reached\033[0m")

        self.anchor = None
        if status == "SUCCESS":  # the session only remembers what worked
            self.recent.append({"intent": intent, "reply": reply, "actions": list(self.done_actions)})
        BRIDGE.emit({"t": "notify", "text": f"Neos {status}: {reply[:120]}",
                     "level": "info" if status == "SUCCESS" else "warn"})


# ==========================================
# 4. INPUT ROUTING
# ==========================================
class Router:
    def __init__(self, agent: NeosAgent):
        self.a = agent

    def accept(self, text: str, sel: Optional[dict], anchor: Optional[int] = None) -> Tuple[str, str]:
        """("keep", text to refill the input line) | ("exit", "") | ("ok", "")."""
        a, clean = self.a, (text or "").strip()
        if a.approval:
            if a.answer_approval(text):
                with a.lock:
                    return "keep", a.approval["detail"] if a.approval else ""
            return "ok", ""
        if not clean:
            return "ok", ""
        if clean.lower() in ("exit", "quit"):
            a.shutdown()
            return "exit", ""
        if clean.startswith("===") or clean.startswith("=-="):
            try:
                self.command(clean)
            except Exception as e:
                chat(f"\033[31m❌ command error: {e}\033[0m")
            return "ok", ""
        if a.busy:
            body = clean[1:].strip() if clean[:1] in "+!" else clean
            if sel:
                steer = f"{body}\n[SELECTION] {sel.get('file')} lines {sel.get('start')}-{sel.get('end')}:\n{sel.get('text', '')}"
            else:
                steer = body
            if clean.startswith("+"):
                a.submit_task(body, sel, anchor)
                chat(dim(f"  ⏳ queued task: {body}"))
            elif clean.startswith("!"):
                a.submit_steer(steer, interrupt=True)
            else:
                a.submit_steer(steer)
                chat(dim("  ↪ steering queued for the next turn"))
        else:
            a.submit_task(clean.lstrip("+!").strip(), sel, anchor)
        return "ok", ""

    def command(self, cmd: str):
        a = self.a
        if cmd == "===h":
            chat(HELP_TEXT)
        elif cmd == "===x":
            a.cancel() if a.busy else chat(dim("nothing is running"))
        elif cmd == "====":
            s, t, n = a.clear_queues()
            chat(dim(f"cleared {s} steering, {t} task(s), {n} load(s)"))
        elif cmd == "===a":
            a.auto = not a.auto
            BRIDGE.mark_dirty()
            chat(dim(f"auto-approve {'ON' if a.auto else 'OFF'}"))
        elif cmd == "===r":
            a.recent.clear()
            chat(dim("recent-session memory cleared"))
        elif cmd == "===c":
            BRIDGE.clear("chat")
        elif cmd.startswith("=-="):
            q = cmd[3:].strip()
            if not q:
                chat(dim("usage: =-= <keywords>"))
                return
            threading.Thread(target=self._search, args=(q,), daemon=True).start()
        elif cmd.startswith("===u"):
            q = cmd[4:].strip()
            if not q:
                chat(dim("usage: ===u <keywords>   (vim: neosu)"))
                return
            threading.Thread(target=self._search, args=(q, ARGS.uvian_load), daemon=True).start()
        elif cmd.startswith("===l"):
            arg = cmd[4:].strip().lstrip("#")
            with a.lock:
                listing = list(a.last_search)
            uid = listing[int(arg) - 1]["uuid"] if arg.isdigit() and 1 <= int(arg) <= len(listing) else arg
            if not (a.uvian and a.uvian.exists(uid)):
                chat(f"\033[31m'{arg}' is not a search result number or an existing uuid\033[0m")
                return
            a.attach(uid)
            chat(dim(f"  📎 {uid} will be loaded {'at the next turn' if a.busy else 'into the next task'}"))
        else:
            chat(dim(f"unknown command {cmd} — ===h for help"))

    def _search(self, q: str, load: int = 0):
        a = self.a
        if not (a.uvian and a.uvian.available):
            chat(f"\033[31muvian search script missing: {a.uvian.script if a.uvian else '(uvian disabled)'}\033[0m")
            return
        try:
            hits = a.uvian.search(q, 20)
        except Exception as e:
            chat(f"\033[31muvian search failed: {e}\033[0m")
            return
        with a.lock:
            a.last_search = hits
        chat(f"\033[1m🔎 uvian:\033[0m {q}  \033[90m({len(hits)} hit(s); Enter on a #n line or ===l <#> loads it)\033[0m")
        for i, h in enumerate(hits, 1):
            mark = " \033[1;32m📎\033[0m" if i <= load else ""
            chat(f" \033[38;5;177m#{i} {h['uuid']}\033[0m \033[90mscore {h['score']}\033[0m{mark}\n     {h['summary']}")
        for h in hits[:load]:
            if "encrypted" not in h["entry"]:
                a.attach(h["uuid"])
        if load and hits:
            chat(dim(f"  📎 top {min(load, len(hits))} loaded into {'the next turn' if a.busy else 'your next question'}"))


# ==========================================
# 5. BOOTSTRAP
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
    print(f"neos: cannot reach the LLM at http://{host}:{port} ({err})", file=sys.stderr)
    return None


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Neos: Neovim operator agent (backend of neos.lua).")
    p.add_argument("--port", type=int, default=8080, help="port of the local LLM server")
    p.add_argument("--host", default="localhost", help="hostname of the local LLM server")
    p.add_argument("--uvian-dir", default="~/echo-home/memory/uvian", help="Uvian memory root")
    p.add_argument("--uvian-load", type=int, default=3, help="uvian hits loaded by ===u / neosu")
    p.add_argument("--vimrc", default="~/.vimrc.neo", help="the only user config whose functions/commands Neos may call")
    p.add_argument("--no-uvian", action="store_true", help="disable Uvian memory")
    p.add_argument("--vim-doc", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_doc.md"),
                   help="user vim setup doc written by :NeosDoc")
    p.add_argument("--doc-top", type=int, default=5, help="vim setup entries added to each task (0 = none)")
    p.add_argument("--auto", action="store_true", help="auto-approve editor actions (also ~/echo-home/autopilot.yaml neos.allow)")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--cmd-timeout", type=int, default=60, help="seconds to wait for Neovim to finish an action")
    p.add_argument("--max-output", type=int, default=8000, help="chars of action output / uvian entry fed to the LLM")
    a = p.parse_args(argv)
    return a


def main():
    global ARGS, CLIENT, MODEL_NAME
    if sys.stdin.isatty():
        print("neos.py is the backend of the Neovim UI. Inside nvim run:\n"
              "  :source ~/echo-home/.vim/mimd/neos/neos.lua   (or :Neos --port 8080)", file=sys.stderr)
    ARGS = parse_args(sys.argv[1:])
    threading.Thread(target=BRIDGE.flusher, daemon=True).start()
    CLIENT = OpenAI(base_url=f"http://{ARGS.host}:{ARGS.port}/v1", api_key="localm")
    MODEL_NAME = get_model_name(ARGS.host, ARGS.port)
    if not MODEL_NAME:
        chat(f"\033[31m❌ cannot reach the LLM at http://{ARGS.host}:{ARGS.port} — start it and run :Neos again\033[0m")
        BRIDGE.flush()
        sys.exit(1)

    uvian = None if ARGS.no_uvian else Uvian(ARGS.uvian_dir)
    vimdoc = VimDoc(ARGS.vim_doc)
    scope = VimrcScope(ARGS.vimrc)
    scope.load()
    agent = NeosAgent(uvian, vimdoc, Policy(scope))
    BRIDGE.state_fn = agent.state
    router = Router(agent)

    chat(f"\033[1;33m◆ Neos\033[0m ready · \033[1m{MODEL_NAME}\033[0m")
    if uvian:
        chat(dim(f"  uvian {uvian.root}: {'ok' if uvian.available else 'search script missing (' + uvian.script + ')'}"))
    chat(dim(f"  vim setup doc {vimdoc.path}: {'ok' if vimdoc.available else 'missing — run :NeosDoc'}"))
    chat(dim(f"  scope: native Neovim + {len(scope.functions)} functions / {len(scope.commands)} commands from {scope.path}"))
    chat(dim(f"  approvals {'auto' if agent.auto else 'ask'} · ===h help"))
    threading.Thread(target=agent.worker, daemon=True).start()

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
                kind, text = router.accept(msg.get("text", ""), msg.get("sel"), msg.get("anchor"))
                if kind == "keep":
                    BRIDGE.emit({"t": "input", "text": text})
                elif kind == "exit":
                    break
            elif t == "interrupt":
                agent.interrupt()
            elif t == "cancel":
                agent.cancel()
            elif t == "clear" and msg.get("b") in Bridge.BUFS:
                BRIDGE.clear(msg["b"])
            elif t == "quit":
                break
            BRIDGE.mark_dirty()
    finally:
        agent.shutdown()
        BRIDGE.emit({"t": "exit"})
        BRIDGE.running = False
        try:
            BRIDGE.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
