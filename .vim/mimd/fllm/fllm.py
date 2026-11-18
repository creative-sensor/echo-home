#!/usr/bin/env python
"""
fllm: a frontier model CLI on the Intelligence Network. Headless backend of fllm.lua (same folder).

A frontier model CLI (claude, codex, gemini, aider ... anything that runs in a console) runs in a Neovim terminal
pane next to the IFACE pane. This process is its iFace: no LLM of its own, only a relay — the frontier's eyes and
mouth on echo-net. The frontier model does the thinking and decides what deserves an answer.

  eyes   echo-net ─► InetClient ─► feed: the IFACE pane + the log file <feed-dir>/<network>--<name>.md
                                    └─► a nudge typed into the frontier's terminal once it is idle:
                                        "[echo-net] 2 new message(s): #14 @memphia → you, #15 @bee. Read them: … read"
  mouth  the frontier runs  fllm_net.py read | say | who | status  (its own shell tool)
                                    ─► control socket (127.0.0.1, token; address in the session file) ─► echo-net

On joining, the iFace types a briefing into the frontier: it is on a network where other agents and humans talk,
where the log is, how to read it (fllm_net.py read) and how to speak (fllm_net.py say). After a CLI restart while
on the network, the briefing is typed again.

The screen tap only serves the typing. Neovim renders the CLI (any CLI works); fllm.lua pushes the visible rows of
the terminal when they change. Screen markers (no LLM) tell the frontier's state: busy (it works: wait), asking (a
permission or menu: the human operator is notified, nothing is typed), ready (its idle footer: type now). A CLI
that never shows a ready marker counts as ready after --quiet seconds of an unchanged screen. While the operator
is in terminal mode in the frontier pane, the iFace never types.

Loop guards: messages from agents deeper than --net-max-depth in a reply chain are logged but do not wake the
frontier; a nudge covers each message once; the frontier may say at most --net-rate messages a minute.

Protocol with fllm.lua: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() -> stderr.

backend -> nvim
  {"t":"style","id":N,"fg","bg","bold","italic","underline","reverse"}
  {"t":"w","b":"iface","s":[[text, style id], ...]}   append to the IFACE pane
  {"t":"clear","b":"iface"}
  {"t":"state","phase","frontier","queue","unread","nudge","operator","alive","attention","feed",
               "net":{joined,connected,name,hub,network,state,members}}
  {"t":"req","id":N,"op":"type","text":..|null,"keys":[..]|null}  -> err "operator" while the human types there
  {"t":"attention","text":..}    the frontier asks something: the operator answers in the terminal
  {"t":"notify","text":..}       shown when the fllm tab is not in view
  {"t":"exit"}

nvim -> backend
  {"t":"screen","rows":[..],"total":N,"height":N,"width":N,"title":..}   the visible rows (sent when they change)
  {"t":"term","op":"start","cmd":..} | {"t":"term","op":"exit","code":N}
  {"t":"operator","active":b}    the human entered / left terminal mode in the frontier pane
  {"t":"input","text":..}        the IFACE input line
  {"t":"res","id":N,"ok":b,"out":..,"err":..}
  {"t":"net","op":"join","name","purpose","hub","network"} | {"t":"net","op":"leave"} | {"t":"net","op":"say","text"}
  {"t":"cancel"} | {"t":"quit"}

Control socket (fllm_net.py -> backend), one JSON line each way:
  {"token","op":"read","last":N|null} | {"token","op":"say","text","to":[..]|null,"re":ID|null}
  {"token","op":"who"} | {"token","op":"status"}            -> {"ok":b,"text":..} | {"ok":false,"error":..}
"""
import argparse
import hmac
import json
import os
import re
import secrets
import shlex
import socketserver
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
for _stream in (sys.stdin, sys.stderr):  # the protocol is UTF-8 whatever the Windows code page is
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ARGS: Optional[argparse.Namespace] = None


# ==========================================
# 1. THE INET DRIVER (mimd/inet/inet_client.py)
# ==========================================
def _path_forms(d: str) -> List[str]:
    """Every spelling of a folder a Windows/Git Bash/MSYS/Cygwin python might understand."""
    s = d.replace("\\", "/")
    forms = [d, s]
    m = re.match(r"^/(?:cygdrive/)?([a-zA-Z])(?:/|$)(.*)", s)
    if m:
        forms.append(f"{m.group(1).upper()}:/{m.group(2)}")
    m = re.match(r"^([a-zA-Z]):/?(.*)", s)
    if m:
        forms += [f"/{m.group(1).lower()}/{m.group(2)}", f"/cygdrive/{m.group(1).lower()}/{m.group(2)}"]
    out = []
    for f in forms:
        f = os.path.normpath(f)
        if f not in out:
            out.append(f)
    return out


def _find_inet():
    tried = []
    for seed in filter(None, [os.environ.get("INET_DIR"), os.path.join(_HERE, "..", "inet")]):
        for d in _path_forms(seed):
            if d in tried:
                continue
            tried.append(d)
            if os.path.isfile(os.path.join(d, "inet_client.py")):
                return d, tried
    return None, tried


INET_DIR, _TRIED = _find_inet()
INET_ERROR = ""
if INET_DIR and INET_DIR not in sys.path:
    sys.path.insert(0, INET_DIR)
try:
    from inet_client import InetClient  # noqa: E402
except ImportError as _e:
    InetClient = None
    INET_ERROR = f"{type(_e).__name__}: {_e}. inet_client.py not found in: " + " | ".join(_TRIED)


# ==========================================
# 2. ANSI -> NVIM HIGHLIGHT BRIDGE (as mksk.py, one pane)
# ==========================================
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_BASIC = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5"]
_BRIGHT = ["#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]
_CTRL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[@-ln-~]|\x1b(?!\[[0-9;]*m)|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")


class SgrState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.fg: Optional[str] = None
        self.bg: Optional[str] = None
        self.bold = self.italic = self.underline = self.reverse = False

    def apply(self, params: str):
        codes = [int(c) if c else 0 for c in params.split(";")] if params else [0]
        for c in codes:
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

    def key(self) -> Optional[tuple]:
        k = (self.fg, self.bg, self.bold, self.italic, self.underline, self.reverse)
        return None if k == (None, None, False, False, False, False) else k


class Bridge:
    """Batches output into ~30 fps protocol writes and routes request/response pairs."""

    FLUSH_INTERVAL = 0.03
    BUFS = ("iface",)

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
                    return {"ok": False, "err": "cancelled"}
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
       "DiagnosticOk": GREEN, "String": GREEN, "Info": CYAN}


def ilog(text: str = "", hl: Optional[str] = None):
    BRIDGE.write("iface", f"{_HL[hl]}{text}{RST}\n" if hl in _HL else f"{text}\n")


def short(s, n: int = 300) -> str:
    """One line, at most n chars."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ==========================================
# 3. THE SCREEN TAP (markers only)
# ==========================================
# Box drawing and block characters: borders of input boxes and panels, not content.
_BORDER_LINE = re.compile(r"^[\s─-▟]+$")
_FRAME_L = re.compile(r"^\s*[│┃║┊┋]\s?")
_FRAME_R = re.compile(r"\s*[│┃║┊┋]$")
# Screen markers of the frontier's state (bottom rows only; --busy/--ask/--ready replace them).
# busy:  Claude Code and Codex "esc to interrupt", Gemini "(esc to cancel, 12s)", braille spinners.
# ask:   a numbered menu with a cursor (permission dialogs), "Do you want to ...?", [y/n].
# ready: the idle footer / input placeholder of Claude Code, Gemini CLI and Codex.
DEFAULT_BUSY = [r"(?i)\besc to interrupt\b", r"(?i)\(esc to cancel, \d+\s*s\)", r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"]
DEFAULT_ASK = [r"^\s*[❯›▶]\s*1[.)]\s", r"(?i)\bdo you want to (proceed|continue|make this edit|create|run|allow)\b",
               r"(?i)[\[(]y/n[\])]"]
DEFAULT_READY = [r"\? for shortcuts", r"(?i)\btype your message\b", r"(?i)\bask codex to do anything\b"]
_OPTION = re.compile(r"^\s*[❯›▶>]?\s*\d+[.)]\s")


def tidy(rows: List[str]) -> List[str]:
    """Screen rows as text: frames and border lines dropped, runs of blank lines folded, trailing blanks gone."""
    out: List[str] = []
    for r in rows:
        r = str(r).rstrip()
        if _BORDER_LINE.match(r):
            r = ""
        else:
            r = _FRAME_R.sub("", _FRAME_L.sub("", r)).rstrip()
        if not r and (not out or not out[-1]):
            continue
        out.append(r)
    while out and not out[-1]:
        out.pop()
    return out


def question_text(rows: List[str], hit: int) -> str:
    """The question around a question marker: from the nearest line ending in '?' above it to its last option line."""
    start = hit
    for i in range(hit, max(-1, hit - 5), -1):
        if rows[i].rstrip().endswith("?"):
            start = i
            break
    end = hit
    for j in range(hit + 1, min(len(rows), hit + 9)):
        if _OPTION.match(rows[j]):
            end = j
    return " / ".join(x.strip() for x in rows[start:end + 1] if x.strip())


class Screen:
    """The latest visible rows of the frontier terminal, pushed by fllm.lua whenever they change."""

    def __init__(self):
        self.cond = threading.Condition()
        self.rows: List[str] = []
        self.seq = 0
        self.changed = time.time()
        self.alive = False
        self.exit_code: Optional[int] = None
        self.operator = False

    def update(self, msg: dict):
        rows = msg.get("rows")
        with self.cond:
            self.rows = [str(r) for r in rows] if isinstance(rows, list) else []
            self.seq += 1
            self.changed = time.time()
            self.cond.notify_all()

    def set(self, **kw):
        with self.cond:
            for k, v in kw.items():
                setattr(self, k, v)
            self.cond.notify_all()

    def snapshot(self) -> Tuple[List[str], int]:
        with self.cond:
            return list(self.rows), self.seq

    def wait(self, pred: Callable[[], bool], timeout: float, cancel: Optional[threading.Event] = None) -> bool:
        end = time.time() + max(0.0, timeout)
        with self.cond:
            while True:
                if pred():
                    return True
                if cancel is not None and cancel.is_set():
                    return False
                left = end - time.time()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 0.2))

    def wait_change(self, seq: int, timeout: float, cancel=None) -> bool:
        return self.wait(lambda: self.seq != seq or not self.alive, timeout, cancel)

    def wait_settled(self, quiet: float, timeout: float, cancel=None) -> bool:
        return self.wait(lambda: time.time() - self.changed >= quiet or not self.alive, timeout, cancel)


# ==========================================
# 4. THE FEED (what the frontier sees of echo-net)
# ==========================================
def fmt_msg(m: dict, me: str, full_date: bool = False) -> str:
    ts = time.localtime(m.get("ts") or time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S" if full_date else "%H:%M", ts)
    frm = "you" if m.get("from") == me else f"@{m.get('from')}"
    to = m.get("to") or []
    dest = (" → " + ", ".join("you" if x == me else "@" + x for x in to) + " (private)") if to else " → room"
    re_ = f", re #{m.get('re')}" if m.get("re") else ""
    body = "\n".join("    " + line for line in str(m.get("text") or "").splitlines()) or "    (empty)"
    return f"#{m.get('id')} {stamp} {frm} ({m.get('kind')}){dest}{re_}:\n{body}"


class Cancelled(Exception):
    pass


class JobError(Exception):
    pass


# ==========================================
# 5. THE iFACE
# ==========================================
class Fllm:
    def __init__(self):
        self.running = True
        self.screen = Screen()
        self.lock = threading.RLock()
        self.queue: deque = deque()     # what waits to be typed: {"kind": "brief"|"note"|"text", "text": ..}
        self.item: Optional[dict] = None
        self.wake = threading.Event()
        self.cancel_ev = threading.Event()
        self.phase = "starting"
        self.frontier = "not started"   # working | asking | ready | quiet | starting | exited | not started
        self.seen_ready = False         # this CLI shows a ready marker when idle: wait for it before typing
        self.attention = ""             # the question on the frontier's screen, escalated to the operator
        self.classified = -1            # screen seq of the last classification
        self.busy_res = self._patterns("busy", ARGS.busy or DEFAULT_BUSY)
        self.ask_res = self._patterns("ask", ARGS.ask or DEFAULT_ASK)
        self.ready_res = self._patterns("ready", ARGS.ready or DEFAULT_READY)
        # the feed
        self.feed: List[dict] = []
        self.feed_keys = set()
        self.read_upto = 0              # feed index the frontier has read up to
        self.nudged_upto = 0            # feed index covered by the nudges typed so far
        self.nudge_at: Optional[float] = None
        self.has_read = False
        self.briefed = False            # the briefing was typed into the running CLI
        self.spoken: deque = deque()    # timestamps of the frontier's own messages (rate limit)
        self.allow = {n.strip().lstrip("@").lower() for n in (ARGS.net_allow or "").split(",") if n.strip()}
        # The command the frontier runs: the script itself when it can run so (a short, stable prefix the CLI's
        # permission rules can allow, e.g. Claude Code --allowedTools "Bash(<path> *)"), else through this python.
        script = os.path.join(_HERE, "fllm_net.py")
        if os.name != "nt" and os.access(script, os.X_OK):
            self.helper = shlex.quote(script)
        else:
            self.helper = " ".join(shlex.quote(x) for x in (sys.executable, script))
        self.token = secrets.token_hex(16)
        self.net = None
        if InetClient is not None:
            self.net = InetClient(
                name=ARGS.net_name or ARGS.name,
                purpose=ARGS.net_purpose or (f"frontier model ({ARGS.cli or ARGS.name}) in a terminal: "
                                             "talk to it like to any member"),
                hub=ARGS.net_hub, network=ARGS.net_network, kind="agent",
                on_message=self._on_message, on_event=self._on_event, log=lambda s: ilog(s, "ErrorMsg"))

    @staticmethod
    def _patterns(kind: str, pats: List[str]) -> list:
        out = []
        for p in pats:
            try:
                out.append(re.compile(p))
            except re.error as e:
                ilog(f"⚠️ bad --{kind} pattern {p!r}: {e}", "ErrorMsg")
        return out

    # ---------------------------------------------------------------- state
    @property
    def me(self) -> str:
        return self.net.name if self.net else (ARGS.net_name or ARGS.name)

    @property
    def joined(self) -> bool:
        return bool(self.net and self.net.joined)

    @property
    def connected(self) -> bool:
        return bool(self.net and self.net.connected)

    def net_summary(self) -> dict:
        n = self.net
        if not n:
            return {"joined": False, "connected": False, "name": self.me, "hub": "unavailable", "network": "",
                    "state": "off", "members": 0}
        return {"joined": n.joined, "connected": n.connected, "name": n.name, "hub": n.hub_str(),
                "network": n.network, "state": (n.state if n.connected else "offline") if n.joined else "off",
                "members": len(n.members)}

    def unread(self) -> List[dict]:
        with self.lock:
            return [m for m in self.feed[self.read_upto:] if m.get("from") != self.me]

    def state(self) -> dict:
        it = self.item
        nudge = None
        if self.nudge_at is not None:
            nudge = max(0, int(self.nudge_at - time.time()))
        return {"t": "state", "phase": self.phase, "frontier": self.frontier,
                "item": it["kind"] if it else None, "queue": len(self.queue) + (1 if it else 0),
                "unread": len(self.unread()), "nudge": nudge, "operator": self.screen.operator,
                "alive": self.screen.alive, "attention": self.attention, "feed": self.feed_path(),
                "net": self.net_summary()}

    def set_phase(self, phase: str):
        if phase != self.phase:
            self.phase = phase
            BRIDGE.mark_dirty()

    def presence(self):
        """The frontier's state, as the other members see it."""
        if not self.connected:
            return
        st = {"working": "working", "asking": "waiting operator"}.get(self.frontier, "listening")
        if self.net.set_status(st):
            BRIDGE.mark_dirty()

    # ---------------------------------------------------------------- the screen
    def classify(self) -> Tuple[str, str]:
        """(state, question) of the frontier from the screen markers."""
        if not self.screen.alive:
            return ("exited" if self.screen.exit_code is not None else "not started"), ""
        rows, _ = self.screen.snapshot()
        raw = [r for r in rows if r.strip()][-ARGS.busy_rows:]
        if any(rx.search(r) for rx in self.busy_res for r in raw):
            return "working", ""
        low = tidy(rows)[-ARGS.busy_rows:]
        for rx in self.ask_res:
            hit = next((i for i, r in enumerate(low) if rx.search(r)), None)
            if hit is not None:
                return "asking", short(question_text(low, hit), 400)
        if any(rx.search(r) for rx in self.ready_res for r in raw):
            self.seen_ready = True
            return "ready", ""
        return ("quiet" if low else "starting"), ""

    def observe(self):
        """Classify the settled screen: frontier state, the operator's attention, presence on the network."""
        with self.lock:  # the watcher and the typist both look
            self._observe()

    def _observe(self):
        seq = self.screen.seq
        if seq == self.classified:
            return
        self.classified = seq
        st, q = self.classify()
        if st != self.frontier:
            self.frontier = st
            BRIDGE.mark_dirty()
        if st == "asking":
            if q != self.attention:
                self.attention = q
                ilog(f"🔐 the frontier asks: {q}\n   ⏸ answer it in the terminal pane", "WarningMsg")
                BRIDGE.emit({"t": "attention", "text": q})
                BRIDGE.mark_dirty()
        elif self.attention:
            self.attention = ""
            BRIDGE.mark_dirty()
        self.presence()

    def watcher(self):
        while self.running:
            if self.screen.wait_settled(ARGS.settle, 0.5):
                self.observe()
            time.sleep(0.2)

    def on_screen(self, msg: dict):
        self.screen.update(msg)
        rows = [r for r in self.screen.rows if r.strip()][-ARGS.busy_rows:]
        if self.frontier != "working" and any(rx.search(r) for rx in self.busy_res for r in rows):
            self.frontier = "working"  # spinners never settle: say it right away
            self.presence()
            BRIDGE.mark_dirty()

    def on_term(self, msg: dict):
        if msg.get("op") == "attach":  # this iFace (re)started next to a CLI that already runs
            self.screen.set(alive=True, exit_code=None)
            ilog(f"🖥  attached to the running frontier: {msg.get('cmd')}", "Comment")
        elif msg.get("op") == "start":
            self.screen.set(alive=True, exit_code=None)
            self.seen_ready, self.briefed, self.classified = False, False, -1
            ilog(f"🖥  frontier started: {msg.get('cmd')}", "Comment")
            if self.joined:
                self.brief()
        elif msg.get("op") == "exit":
            self.screen.set(alive=False, exit_code=msg.get("code"))
            self.frontier, self.briefed = "exited", False
            ilog(f"🖥  frontier exited (code {msg.get('code')}) — :FllmRestart starts it again", "WarningMsg")
        BRIDGE.mark_dirty()

    def on_operator(self, active: bool):
        self.screen.set(operator=bool(active))
        BRIDGE.mark_dirty()

    # ---------------------------------------------------------------- typing into the frontier
    def check_cancel(self):
        if self.cancel_ev.is_set():
            raise Cancelled()

    def press(self, text: Optional[str] = None, keys: Optional[List[str]] = None, job: bool = True) -> dict:
        """Type into the frontier terminal; waits while the operator is in terminal mode there."""
        ev = self.cancel_ev if job else None
        told = False
        while True:
            if job:
                self.check_cancel()
            res = BRIDGE.request("type", ARGS.cmd_timeout, cancel=ev, text=text, keys=keys)
            if res.get("ok"):
                return res.get("out") or {}
            err = str(res.get("err") or "")
            if err == "cancelled":
                raise Cancelled()
            if err != "operator":
                raise JobError(f"cannot type into the terminal: {err}")
            if not told:
                told = True
                ilog("   ⏸ you are in terminal mode in the frontier pane: I type once you leave it (<C-\\><C-n>)",
                     "WarningMsg")
                if job:
                    self.set_phase("waiting operator")
            self.screen.wait(lambda: not self.screen.operator, 1.0, ev)

    def press_keys(self, keys: List[str], job: bool = True):
        for i, k in enumerate(keys):
            if i:
                time.sleep(ARGS.key_delay)
            self.press(keys=[k], job=job)

    def wait_ready(self):
        """Until the frontier waits for a message: its ready marker, or (a CLI without one) a quiet screen."""
        told = ""
        while True:
            self.check_cancel()
            if not self.screen.alive:
                if self.screen.exit_code is not None:
                    raise JobError("the frontier CLI is not running (:FllmRestart)")
                self.set_phase("waiting (CLI not started)")  # the iFace came up first: the CLI follows
                self.screen.wait(lambda: self.screen.alive, 1.0, self.cancel_ev)
                continue
            if not self.screen.wait_settled(ARGS.settle, 1.0, self.cancel_ev):
                continue
            self.observe()
            st = self.frontier
            if st == "ready":
                return
            if st == "quiet" and not self.seen_ready and time.time() - self.screen.changed >= ARGS.quiet:
                return
            if st != told:
                told = st
                why = {"working": "it works", "asking": "it asks you something (terminal pane)",
                       "quiet": "no idle marker on its screen yet", "starting": "it starts"}.get(st, st)
                ilog(f"   ⏳ waiting to type: {why}", "Comment")
                self.set_phase(f"waiting ({st})")
            self.screen.wait_change(self.screen.seq, 1.0, self.cancel_ev)

    def type_message(self, text: str):
        """Type a message into the frontier and submit it (bracketed paste for several lines, as a human pastes)."""
        seq = self.screen.seq
        self.set_phase("typing")
        self.press(text=text)
        self.screen.wait_change(seq, 3.0, self.cancel_ev)
        self.check_cancel()
        time.sleep(ARGS.submit_delay)  # some TUIs take text + Enter in one burst as a paste
        self.screen.wait_settled(0.3, 2.0, self.cancel_ev)
        seq = self.screen.seq
        self.press(keys=["<Enter>"])
        if not self.screen.wait_change(seq, ARGS.submit_check, self.cancel_ev):
            self.check_cancel()
            ilog("   ↻ no reaction to Enter: pressing it again", "Comment")
            self.press(keys=["<Enter>"])

    def enqueue(self, kind: str, text: str, front: bool = False):
        with self.lock:
            if kind == "brief":  # one briefing at a time
                self.queue = deque(x for x in self.queue if x["kind"] != "brief")
            (self.queue.appendleft if front else self.queue.append)({"kind": kind, "text": text})
        self.wake.set()
        BRIDGE.mark_dirty()

    def next_item(self) -> Optional[dict]:
        with self.lock:
            if self.queue:
                return self.queue.popleft()
            if self.nudge_at is not None and time.time() >= self.nudge_at:
                return {"kind": "nudge", "text": ""}
        return None

    def worker(self):
        while self.running:
            item = self.next_item()
            if not item:
                if self.nudge_at is not None:
                    self.set_phase(f"nudge in {max(0, int(self.nudge_at - time.time()))}s")
                elif not self.screen.operator:
                    self.set_phase("idle")
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            self.cancel_ev.clear()
            self.item = item
            BRIDGE.mark_dirty()
            try:
                self.wait_ready()
                if item["kind"] == "nudge":
                    text, upto = self.nudge_text()
                    with self.lock:
                        self.nudge_at = None
                    if not text:
                        continue  # the frontier read everything by itself meanwhile
                    self.type_message(text)
                    with self.lock:
                        self.nudged_upto = max(self.nudged_upto, upto)
                    ilog(f"   👉 nudged: {text}", "Info")
                else:
                    self.type_message(item["text"])
                    if item["kind"] == "brief":
                        self.briefed = True
                        ilog("   📋 briefing typed: the frontier knows it is on the network", "Info")
                    else:
                        ilog(f"   ⌨  typed: {short(item['text'], 200)}", "Comment")
            except Cancelled:
                ilog(f"   ✋ dropped: {item['kind']}", "Comment")
            except JobError as e:
                ilog(f"   ⚠️ {item['kind']} not typed: {e}", "ErrorMsg")
                if item["kind"] == "nudge":
                    with self.lock:
                        self.nudge_at = None
            except Exception as e:
                ilog(f"❌ {type(e).__name__}: {e}", "ErrorMsg")
            finally:
                self.item = None
                BRIDGE.mark_dirty()

    def cancel(self) -> bool:
        if self.item:
            self.cancel_ev.set()
            return True
        return False

    def drop_queue(self) -> int:
        with self.lock:
            n = len(self.queue)
            self.queue.clear()
            self.nudge_at = None
        BRIDGE.mark_dirty()
        return n

    # ---------------------------------------------------------------- briefing and nudges
    def feed_path(self) -> str:
        net = re.sub(r"[^\w.-]", "-", (self.net.network if self.net else "") or ARGS.net_network or "echo-net")
        name = re.sub(r"[^\w.-]", "-", self.me)
        return os.path.join(os.path.expanduser(ARGS.feed_dir), f"{net}--{name}.md")

    def briefing(self) -> str:
        n = self.net
        others = [f"@{k} ({m.get('kind')}{': ' + short(m.get('purpose'), 80) if m.get('purpose') else ''})"
                  for k, m in n.members.items() if k != n.name]
        h = self.helper
        extra = f"\n{ARGS.brief.strip()}" if ARGS.brief.strip() else ""
        return (
            f"[echo-net] You are now connected to {n.network or 'echo-net'}, an Intelligence Network: a chat room where "
            f"AI agents and humans talk to each other and hand each other work. You are on it as @{n.name}"
            f"{' — your purpose there: ' + n.purpose if n.purpose else ''}.\n"
            f"Members now: {', '.join(others) or 'nobody else yet'}.\n"
            "I am your iFace: I only relay. I never answer for you, and nothing reaches the network unless you send it.\n"
            f"- Read: when something new arrives I type a line starting with [echo-net]. Then run:  {h} read\n"
            f"  (it shows what is new since you last read; add --last 30 for recent history). Everything is also "
            f"logged in {self.feed_path()}\n"
            f"- Speak:  {h} say \"text\"   — no @name: the whole room hears it; @name in the text, or --to NAME: "
            "private to them; --re ID: a reply to message #ID (private if #ID was private to you). Long or multi-line "
            f"text: {h} say --re ID - <<'EOF' ... EOF\n"
            f"- Who is there:  {h} who\n"
            "- You decide what needs an answer. Messages addressed to you deserve one; chatter between others and plain "
            "thanks or acknowledgements do not. Do not reply just to agree or thank: two agents doing that never stop. "
            "Send results and conclusions, short and concrete; put long material in a file and send its path.\n"
            "- Work asked on the network gets the same care as work asked here. Destructive or risky steps, or anything "
            "touching secrets, need the OK of the human in this terminal first.\n"
            f"Run the read command now once to see what has been said so far.{extra}")

    def brief(self):
        if not self.net or not self.net.connected:
            return
        self.enqueue("brief", self.briefing(), front=True)

    def nudge_text(self) -> Tuple[str, int]:
        with self.lock:
            start = max(self.nudged_upto, self.read_upto)
            new = [m for m in self.feed[start:] if m.get("from") != self.me]
            upto = len(self.feed)
        if not new:
            return "", upto
        parts = []
        for m in new[:6]:
            to = m.get("to") or []
            tag = " → you" if to else (" (mentions you)" if self.me in (m.get("mentions") or []) else "")
            parts.append(f"#{m.get('id')} @{m.get('from')}{tag}")
        more = f" and {len(new) - 6} more" if len(new) > 6 else ""
        return (f"[echo-net] {len(new)} new message(s): {', '.join(parts)}{more}. Read them: {self.helper} read",
                upto)

    def schedule_nudge(self, delay: float):
        with self.lock:
            when = time.time() + delay
            self.nudge_at = when if self.nudge_at is None else min(self.nudge_at, when)
        self.wake.set()
        BRIDGE.mark_dirty()

    # ---------------------------------------------------------------- echo-net
    def join(self, name=None, purpose=None, hub=None, network=None):
        if self.net is None:
            return ilog(f"⚠️ cannot join: Intelligence Network driver not loaded — {INET_ERROR}", "ErrorMsg")
        was = self.net.joined
        self.net.join(name=name, purpose=purpose, hub=hub, network=network)
        if not was:
            ilog(f"🌐 joining {self.net.network or 'network'} at {self.net.hub_str()} as @{self.net.name}", "Title")
            ilog(f"   purpose: {self.net.purpose}", "Comment")
        BRIDGE.mark_dirty()

    def leave(self):
        if not self.joined:
            return ilog("ℹ️ not on the network", "Comment")
        self.net.leave()
        if self.briefed:
            self.enqueue("note", "[echo-net] You left echo-net: do not read or post there until I tell you that you "
                                 "are back.")
            self.briefed = False
        with self.lock:
            self.nudge_at = None
        BRIDGE.mark_dirty()

    def _on_event(self, kind: str, data: dict):
        n = self.net
        if kind == "welcome":
            others = [f"@{k}" for k in n.members if k != n.name]
            ilog(f"🌐 on {n.network} as @{n.name} · members: {', '.join(others) or 'nobody else yet'}", "DiagnosticOk")
            ilog(f"   log: {self.feed_path()}", "Comment")
            n.state = ""
            fresh = 0
            for m in data.get("history") or []:
                direct = n.name in (m.get("to") or [])
                if m.get("to") and not direct and m.get("from") != n.name:
                    continue
                fresh += self.add_feed(m)
            if fresh:
                ilog(f"   {fresh} earlier message(s) in the log", "Comment")
            self.presence()
            if not self.briefed:
                self.brief()
        elif kind in ("join", "leave"):
            m = data.get("member") or {}
            if m.get("name") != n.name:
                ilog(f"   {'→' if kind == 'join' else '←'} @{m.get('name')} {'joined' if kind == 'join' else 'left'}"
                     f"{' — ' + short(m.get('purpose'), 100) if m.get('purpose') else ''}", "Comment")
        elif kind == "error":
            ilog(f"⚠️ hub: {data.get('text')}", "ErrorMsg")
        elif kind == "left":
            ilog("👋 left the network", "Title")
        elif kind == "disconnected" and data.get("reason") == "lost the hub":
            ilog("⚠️ lost the hub; reconnecting…", "ErrorMsg")
        BRIDGE.mark_dirty()

    def add_feed(self, m: dict) -> int:
        """Into the feed and its log file, once (1 if it was new)."""
        key = (m.get("id"), m.get("ts"))
        with self.lock:
            if key in self.feed_keys:
                return 0
            self.feed_keys.add(key)
            self.feed.append(m)
        path = self.feed_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write(f"# echo-net log of @{self.me} — what it hears and says on the network\n\n")
                f.write(fmt_msg(m, self.me, full_date=True) + "\n\n")
        except OSError as e:
            ilog(f"⚠️ cannot write the log {path}: {e}", "ErrorMsg")
        BRIDGE.mark_dirty()
        return 1

    def _on_message(self, msg: dict):
        """A message from another member (reader thread: keep it short)."""
        direct, mentioned = self.net.addressing(msg)
        if msg.get("to") and not direct:
            return  # someone else's private exchange
        if not self.add_feed(msg):
            return
        how = "→ you (private)" if direct else "mentions you" if mentioned else "room"
        stamp = time.strftime("%H:%M:%S", time.localtime(msg.get("ts") or time.time()))
        ilog(f"\n{stamp} 📨 #{msg.get('id')} @{msg.get('from')} [{how}, {msg.get('kind')}, depth {msg.get('depth', 0)}]",
             "Special")
        ilog("   " + str(msg.get("text") or "").replace("\n", "\n   "))
        addressed = direct or mentioned
        sender = str(msg.get("from") or "")
        if ARGS.nudge == "off":
            return
        if self.allow and sender.lower() not in self.allow:
            return ilog("   (logged; only --net-allow members wake the frontier)", "Comment")
        if msg.get("kind") == "agent" and int(msg.get("depth") or 0) >= ARGS.net_max_depth:
            return ilog(f"   ⛔ reply chain depth {msg.get('depth')} ≥ {ARGS.net_max_depth}: logged, not waking the "
                        "frontier", "Comment")
        if ARGS.nudge == "addressed" and not addressed:
            return
        self.schedule_nudge(0 if addressed else ARGS.batch)

    def member(self, name: str) -> Optional[str]:
        name = (name or "").lstrip("@").lower()
        for n in (self.net.members if self.net else {}):
            if n.lower() == name:
                return n
        return None

    def who_text(self) -> str:
        n = self.net
        if not n or not n.connected:
            return "You are not on echo-net right now."
        lines = [f"{n.network} — {len(n.members)} member(s):"]
        for k, m in n.members.items():
            lines.append(f"  {'◆' if m.get('kind') == 'agent' else '●'} @{k}{' (you)' if k == n.name else ''} "
                         f"[{m.get('kind')}, {m.get('state') or '-'}] {short(m.get('purpose'), 160)}")
        return "\n".join(lines)

    def say(self, text: str, to: Optional[List[str]] = None, re_id=None, who: str = "frontier") -> Tuple[bool, str]:
        """Post on echo-net as this member. to=None: the hub routes by @mentions."""
        n = self.net
        text = (text or "").strip()
        if not text:
            return False, "nothing to say (empty text)"
        if not n or not n.connected:
            return False, "not on echo-net right now (the operator joins with :InetJoin)"
        if len(text) > 20000:
            return False, f"too long ({len(text)} chars; at most 20000): put it in a file and send its path"
        if who == "frontier":
            now = time.time()
            while self.spoken and now - self.spoken[0] > 60:
                self.spoken.popleft()
            if len(self.spoken) >= ARGS.net_rate:
                return False, f"rate limit: {len(self.spoken)} messages in the last minute (at most {ARGS.net_rate}); wait"
        names = None
        if to:
            names = []
            for t in to:
                c = self.member(t)
                if not c:
                    return False, f"@{str(t).lstrip('@')} is not on the network. {self.who_text()}"
                names.append(c)
        try:
            re_id = int(re_id) if re_id not in (None, "") else None
        except (TypeError, ValueError):
            return False, f"--re takes a message number, not {re_id!r}"
        if re_id is not None and names is None and "@" not in text:
            orig = next((m for m in self.feed if m.get("id") == re_id), None)
            if orig and orig.get("to") and orig.get("from") != n.name and self.member(orig.get("from")):
                names = [self.member(orig.get("from"))]  # a private request gets a private answer
        if not n.say(text, re=re_id, to=names):
            return False, "send failed (connection lost?)"
        if who == "frontier":
            self.spoken.append(time.time())
        sent = None
        end = time.time() + 1.5
        while time.time() < end and sent is None:  # the hub echoes our message back: its id and route
            for m in reversed(list(n.transcript)):
                if m.get("from") == n.name and m.get("text") == text and (m.get("id"), m.get("ts")) not in self.feed_keys:
                    sent = m
                    break
            else:
                time.sleep(0.05)
        if sent:
            self.add_feed(sent)
            with self.lock:
                self.read_upto = max(self.read_upto, len(self.feed))
        route = sent.get("to") if sent else names
        dest = f"→ {', '.join('@' + x for x in route)} " if route else "→ room "
        ilog(f"   💬 @{n.name} {dest}{'(#' + str(sent.get('id')) + ') ' if sent else ''}"
             + text.replace("\n", "\n      "), "String")
        return True, (f"sent #{sent.get('id')} " if sent else "sent ") + (
            f"privately to {', '.join('@' + x for x in route)}" if route else "to the whole room")

    # ---------------------------------------------------------------- control socket (fllm_net.py)
    def control(self, req: dict) -> dict:
        op = req.get("op")
        if op == "read":
            return {"ok": True, "text": self.read_text(req.get("last"))}
        if op == "say":
            to = req.get("to")
            ok, text = self.say(str(req.get("text") or ""), to=list(to) if to else None, re_id=req.get("re"))
            return {"ok": True, "text": text} if ok else {"ok": False, "error": text}
        if op == "who":
            return {"ok": True, "text": self.who_text()}
        if op == "status":
            s = self.net_summary()
            return {"ok": True, "text": (f"@{s['name']} {'on' if s['connected'] else 'not on'} {s['network'] or 'echo-net'} "
                                         f"({s['hub']}), {s['members']} member(s), {len(self.unread())} unread; "
                                         f"log: {self.feed_path()}")}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def read_text(self, last=None) -> str:
        me = self.me
        with self.lock:
            if last:
                msgs = self.feed[-max(1, int(last)):]
                older = 0
            else:
                msgs = [m for m in self.feed[self.read_upto:] if m.get("from") != me]
                older = 0
                if not self.has_read and len(msgs) > ARGS.first_read:
                    older, msgs = len(msgs) - ARGS.first_read, msgs[-ARGS.first_read:]
            self.read_upto = len(self.feed)
            self.nudged_upto = max(self.nudged_upto, self.read_upto)
            self.has_read = True
            self.nudge_at = None
        BRIDGE.mark_dirty()
        n = self.net
        where = f"{n.network} as @{me}" if n and n.connected else "echo-net (you are not connected right now)"
        if not msgs:
            ilog(f"   👀 @{me} read: nothing new", "Comment")
            return f"{where}: no new messages."
        ilog(f"   👀 @{me} read {len(msgs)} message(s)", "Info")
        head = (f"{where} — the last {len(msgs)} message(s):" if last else f"{where} — {len(msgs)} new message(s)"
                + (f" ({older} older in the log)" if older else "") + ":")
        body = "\n\n".join(fmt_msg(m, me) for m in msgs)
        return (f"{head}\n\n{body}\n\nAnswer only what needs an answer:  {self.helper} say --re ID \"text\"   "
                f"(log: {self.feed_path()})")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            req = json.loads(self.rfile.readline(1 << 22) or b"{}")
            if not hmac.compare_digest(str(req.get("token") or ""), AGENT.token):
                res = {"ok": False, "error": "bad token (an old session file?)"}
            else:
                res = AGENT.control(req)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            self.wfile.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


AGENT: Optional[Fllm] = None


def write_session(path: str, port: int):
    """Where fllm_net.py finds this iFace: address and token, readable by this user only."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"name": ARGS.name, "host": "127.0.0.1", "port": port, "token": AGENT.token,
                   "pid": os.getpid(), "feed": AGENT.feed_path()}, f)
    os.replace(tmp, path)


# ==========================================
# 6. THE OPERATOR (IFACE input line)
# ==========================================
HELP_TEXT = f"""{BOLD}fllm iFace{RST} — the frontier's eyes and mouth on echo-net. I relay; the frontier thinks.
  <text>              type <text> into the frontier (when it is ready)
  ===b                type the network briefing again        ===r  nudge it to read now
  ===k <keys>         press keys in the frontier, e.g. ===k 1 <Enter>   (<Esc> <Up> <Down> <Tab> <Space> <C-c>)
  ===x                drop what I am about to type           ====  drop everything queued
  ===c                clear this pane                        ===h  this help
  ===n                network status   ===nj [name] [purpose] join   ===nl leave   ===nw who   ===nf the log file
  ===ns <text>        say it on the network as the frontier (@name = point-to-point)
The frontier reads and speaks with:  fllm_net.py read | say | who | status   (told in the briefing)
{BOLD}Keys{RST}  IFACE: <CR> send │ C-c drop / clear the line │ <Tab> (normal) the terminal │ q back
      TERMINAL: a real terminal — use the CLI yourself any time; while you are in terminal mode I never type.
      <C-\\><C-n> leaves terminal mode, <Tab> (normal) comes back here."""


class Router:
    def __init__(self, agent: Fllm):
        self.a = agent

    def accept(self, text: str):
        """Runs on the stdin reader thread: never waits on Neovim here."""
        a, clean = self.a, (text or "").strip()
        if not clean:
            return
        if not clean.startswith("==="):
            a.enqueue("text", clean)
            if a.item or len(a.queue) > 1:
                ilog(f"   ⏳ queued to type ({len(a.queue)} waiting)", "Comment")
            return
        head, _, arg = clean.partition(" ")
        arg = arg.strip()
        try:
            if head == "===h":
                ilog(HELP_TEXT)
            elif head == "===b":
                if a.connected:
                    a.brief()
                else:
                    ilog("ℹ️ not on the network: nothing to brief (===nj joins)", "Comment")
            elif head == "===r":
                with a.lock:
                    a.nudged_upto = a.read_upto  # cover everything unread again
                a.schedule_nudge(0)
                if not a.unread():
                    a.enqueue("note", f"[echo-net] Have a look at echo-net: {a.helper} read --last 10")
            elif head == "===x":
                ilog("   ✋ dropping what I was about to type" if a.cancel() else "   nothing is being typed", "Comment")
            elif head == "====":
                ilog(f"   dropped {a.drop_queue()} queued item(s)", "Comment")
            elif head == "===c":
                BRIDGE.clear("iface")
            elif head == "===k":
                if not arg:
                    ilog("usage: ===k <keys>   e.g. ===k 1 <Enter>", "Comment")
                else:
                    threading.Thread(target=self._keys, args=(arg.split(),), daemon=True).start()
            elif head == "===n":
                s = a.net_summary()
                ilog(f"🌐 {'joined' if s['joined'] else 'not joined'}, {'connected' if s['connected'] else 'disconnected'} "
                     f"to {s['network'] or 'network'} ({s['hub']}) as @{s['name']} [{s['state']}], "
                     f"{s['members']} member(s), {len(a.unread())} unread by the frontier\n   log: {a.feed_path()}")
            elif head == "===nj":
                name, _, purpose = arg.partition(" ")
                a.join(name or None, purpose.strip() or None)
            elif head == "===nl":
                a.leave()
            elif head == "===nw":
                ilog(a.who_text())
            elif head == "===nf":
                ilog(f"   log: {a.feed_path()}")
            elif head == "===ns":
                if not arg:
                    ilog("usage: ===ns <text>", "Comment")
                else:
                    threading.Thread(target=self._say, args=(arg,), daemon=True).start()
            else:
                ilog(HELP_TEXT)
        except Exception as e:
            ilog(f"❌ command error: {type(e).__name__}: {e}", "ErrorMsg")

    def _keys(self, keys: List[str]):
        try:
            self.a.press_keys(keys, job=False)
        except Exception as e:
            ilog(f"❌ {e}", "ErrorMsg")

    def _say(self, text: str):
        ok, res = self.a.say(text, who="operator")
        if not ok:
            ilog(f"⚠️ {res}", "ErrorMsg")


# ==========================================
# 7. MAIN
# ==========================================
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="fllm: a frontier model CLI on the Intelligence Network — its eyes and "
                                            "mouth (backend of fllm.lua; no LLM of its own).")
    p.add_argument("--name", default="fllm", help="session (profile) name")
    p.add_argument("--cli", default="", help="what runs in the terminal, e.g. 'Claude Code'")
    p.add_argument("--session", default="", help="session file for fllm_net.py (address and token of this iFace)")
    p.add_argument("--feed-dir", default="~/echo-home/Documents/logixa/fllm",
                   help="folder of the echo-net log the frontier reads (<network>--<name>.md)")
    p.add_argument("--brief", default="", help="extra lines for the briefing (the profile's own instructions)")
    p.add_argument("--nudge", choices=("all", "addressed", "off"), default="all",
                   help="which messages make me tell the frontier to read: all, only those addressed to it, none")
    p.add_argument("--batch", type=float, default=20.0,
                   help="seconds room messages gather before one nudge (addressed ones nudge right away)")
    p.add_argument("--first-read", type=int, default=20, help="messages the first read shows (older: in the log)")
    p.add_argument("--busy", action="append", default=[],
                   help="regex meaning 'the frontier is working' on the bottom rows (repeatable; replaces defaults)")
    p.add_argument("--ask", action="append", default=[],
                   help="regex meaning 'the frontier asks a question' (repeatable; replaces defaults)")
    p.add_argument("--ready", action="append", default=[],
                   help="regex meaning 'the frontier waits for a message' (repeatable; replaces defaults)")
    p.add_argument("--busy-rows", type=int, default=12, help="bottom non-empty rows searched for the markers")
    p.add_argument("--settle", type=float, default=1.5, help="seconds without a screen change before reading it")
    p.add_argument("--quiet", type=float, default=5.0,
                   help="a CLI that never showed a ready marker counts as ready after this many quiet seconds")
    p.add_argument("--submit-delay", type=float, default=0.4, help="seconds between typing a message and Enter")
    p.add_argument("--submit-check", type=float, default=6.0, help="seconds to see the screen react to Enter")
    p.add_argument("--key-delay", type=float, default=0.15, help="seconds between keys of ===k")
    p.add_argument("--cmd-timeout", type=int, default=20, help="seconds to wait for Neovim to type")
    g = p.add_argument_group("Intelligence Network")
    g.add_argument("--net-hub", default=os.environ.get("FLLM_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4,
                   help="agent reply chains this deep are logged but do not wake the frontier")
    g.add_argument("--net-rate", type=int, default=10, help="messages the frontier may send a minute")
    g.add_argument("--net-allow", default="", help="comma list of members whose messages wake the frontier "
                                                   "(default: everyone; the others are only logged)")
    return p.parse_args(argv)


def main():
    global ARGS, AGENT
    if sys.stdin.isatty():
        print("fllm.py is the backend of the Neovim UI. Inside nvim run:\n"
              "  :source ~/echo-home/.vim/mimd/fllm/fllm.lua   (or :Fllm claude)", file=sys.stderr)
    ARGS = parse_args(sys.argv[1:])
    threading.Thread(target=BRIDGE.flusher, daemon=True).start()
    AGENT = agent = Fllm()
    BRIDGE.state_fn = agent.state
    router = Router(agent)

    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if ARGS.session:
        write_session(ARGS.session, server.server_address[1])

    ilog(f"\033[1;36m◆ fllm\033[0m iFace of \033[1m{ARGS.cli or ARGS.name}\033[0m — its eyes and mouth on echo-net "
         "(a relay: no LLM of its own)")
    if INET_ERROR:
        ilog("⚠️ Intelligence Network driver not loaded; the frontier cannot join.", "ErrorMsg")
        ilog("   " + INET_ERROR, "ErrorMsg")
    ilog(f"{DIM}  :InetJoin {ARGS.name} joins the network and briefs the frontier · text here is typed into it · "
         f"===h help{RST}")
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.watcher, daemon=True).start()
    if ARGS.net_name:
        agent.join()

    try:
        for line in iter(sys.stdin.readline, ""):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            t = msg.get("t")
            if t == "screen":
                agent.on_screen(msg)
            elif t == "res":
                BRIDGE.resolve(msg)
            elif t == "input":
                router.accept(msg.get("text", ""))
            elif t == "term":
                agent.on_term(msg)
            elif t == "operator":
                agent.on_operator(msg.get("active"))
            elif t == "cancel":
                if not agent.cancel():
                    ilog("   nothing is being typed", "Comment")
            elif t == "net":
                op = msg.get("op")
                if op == "join":
                    agent.join(msg.get("name") or None, msg.get("purpose") or None,
                               msg.get("hub") or None, msg.get("network") or None)
                elif op == "leave":
                    agent.leave()
                elif op == "say":
                    threading.Thread(target=agent.say, args=(msg.get("text", ""),), kwargs={"who": "operator"},
                                     daemon=True).start()
            elif t == "quit":
                break
    finally:
        agent.running = False
        agent.cancel_ev.set()
        if agent.net:
            try:
                agent.net.close()
            except Exception:
                pass
        server.shutdown()
        if ARGS.session:
            try:
                with open(ARGS.session, encoding="utf-8") as f:
                    if json.load(f).get("pid") == os.getpid():
                        os.remove(ARGS.session)
            except (OSError, ValueError):
                pass
        BRIDGE.emit({"t": "exit"})
        BRIDGE.running = False
        try:
            BRIDGE.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
