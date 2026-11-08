"""
Memphia core: the UI-independent agentic engine (Uvian memory, Memgraph relation graph,
map-reduce summarizer, streamed process execution, agent loop and input routing).

Frontends
  memphia.py                    full-screen prompt_toolkit TUI
  memphia.lua + memphia_nvim.py the same TUI inside Neovim

Intelligence Network (optional; the shared driver lives in mimd/inet, set up by mimd/vimpage_inet)
  inet/inet_hub.py              the network: the chat room where agents and humans meet
  inet/inet_client.py           reusable agent connection (memphia_iface.py builds on it)
  inet/inet.lua, inet_room.lua  Neovim driver (:InetUp/:InetJoin/:InetRoom) and the room tab
  memphia_iface.py              Memphia's network interface (own loop, IFACE pane)

A frontend calls bootstrap(), registers a redraw hook with set_redraw(), renders BUFFERS
(OutputBuffer.subscribe() for push updates) and feeds input lines to CommandRouter.accept().

Input routing
  idle  : text starts a new task
  busy  : text becomes a steering message injected at the next turn
          "!text" steers AND interrupts the current LLM generation immediately
          "+text" queues a new task to run after the current one
  approval pending : y / n / e (edit the command in the input line)

Commands: type ===h inside the TUI.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

import requests
import yaml
from openai import OpenAI

try:
    from neo4j import GraphDatabase  # Memgraph speaks Bolt; the neo4j driver works with it.
except ImportError:
    GraphDatabase = None

ARGS: Optional[argparse.Namespace] = None
CLIENT: Optional[OpenAI] = None
MODEL_NAME: Optional[str] = None
HISTORY_FILE = ".memphia.py.history"
CURRENT_CWD = ""

# ==========================================
# 1. GIT BASH & WINDOWS PATH RESOLVER
# ==========================================
def resolve_path(path_str: str) -> str:
    if not path_str:
        return path_str
    if path_str.startswith("~"):
        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        path_str = os.path.join(home_dir, path_str[1:].lstrip("/\\"))
    if platform.system() == "Windows" or os.environ.get("MSYSTEM"):
        match = re.match(r"^/([a-zA-Z])/(.*)", path_str)
        if match:
            drive, rest = match.groups()
            path_str = f"{drive.upper()}:/{rest}"
    return os.path.normpath(path_str).replace("\\", "/")


def to_bash_path(path_str: str) -> str:
    if not path_str:
        return path_str
    path_str = path_str.replace("\\", "/")
    match = re.match(r"^([a-zA-Z]):/(.*)", path_str)
    if match:
        drive, rest = match.groups()
        return f"/{drive.lower()}/{rest}"
    return path_str


def is_windows() -> bool:
    return platform.system() == "Windows" or os.environ.get("OS") == "Windows_NT"


def parse_llm_json(raw_text: str) -> Optional[dict]:
    """Extract a JSON object from LLM output. Returns None when nothing parseable is found."""
    clean = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw_text or "", flags=re.DOTALL)
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?", "", clean).strip()
    try:
        obj = json.loads(clean)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(clean[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# ==========================================
# 2. TUI OUTPUT BUFFERS
# ==========================================
_REDRAW: Callable[[], None] = lambda: None


def set_redraw(fn: Callable[[], None]):
    """Frontend hook called whenever buffers or agent state change."""
    global _REDRAW
    _REDRAW = fn


def redraw():
    _REDRAW()


_NON_SGR_CSI = re.compile(r"\x1b\[[0-9;?]*[@-ln-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OTHER_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
# Any ESC not starting a complete SGR sequence; prompt_toolkit's ANSI parser would eat the next char (even '\n').
_STRAY_ESC = re.compile(r"\x1b(?!\[[0-9;]*m)")


def _sanitize(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = _OSC.sub("", text)
    text = _NON_SGR_CSI.sub("", text)
    text = _STRAY_ESC.sub("", text)
    return _OTHER_CTRL.sub("", text).replace("\t", "    ")


def _collapse_cr(line: str) -> str:
    # Progress bars rewrite the line with '\r'; keep only the latest state.
    if "\r" not in line:
        return line
    segs = [s for s in line.split("\r") if s]
    return segs[-1] if segs else ""


class OutputBuffer:
    """Thread-safe append-only text buffer with ANSI colours, tail-follow and scroll-back."""

    def __init__(self, key: str, title: str, max_lines: int = 5000):
        self.key, self.title, self.max_lines = key, title, max_lines
        self._lines: List[str] = [""]
        self._lock = threading.RLock()
        self._version = 0
        self._listeners: List[Callable[[str, Optional[str]], None]] = []
        self.scroll_line: Optional[int] = None  # None => follow tail
        self.unread = False

    def write(self, text: str):
        if not text:
            return
        text = _sanitize(str(text))
        with self._lock:
            parts = text.split("\n")
            self._lines[-1] += parts[0]
            self._lines.extend(parts[1:])
            overflow = len(self._lines) - self.max_lines
            if overflow > 0:
                del self._lines[:overflow]
                if self.scroll_line is not None:
                    self.scroll_line = max(0, self.scroll_line - overflow)
            self._version += 1
            self.unread = True
            for fn in self._listeners:  # under the lock so listeners see writes in buffer order
                fn(self.key, text)
        _REDRAW()

    def writeln(self, text: str = ""):
        # Colour each line separately so a style never bleeds across a cut render window.
        lines = str(text).split("\n")
        m = re.match(r"^((?:\x1b\[[0-9;]*m)+)", lines[0])
        if m and len(lines) > 1:
            prefix = m.group(1)
            lines = [lines[0] + "\x1b[0m"] + [prefix + l + "\x1b[0m" for l in lines[1:]]
        self.write("\n".join(lines) + "\n")

    def clear(self):
        with self._lock:
            self._lines = [""]
            self.scroll_line = None
            self._version += 1
            for fn in self._listeners:
                fn(self.key, None)
        _REDRAW()

    def subscribe(self, fn: Callable[[str, Optional[str]], None]):
        """fn(key, text) gets every sanitized write; text is None when the buffer is cleared."""
        with self._lock:
            self._listeners.append(fn)

    def _target_line(self) -> int:
        last = len(self._lines) - 1
        if last > 0 and self._lines[-1] == "":
            last -= 1
        return last if self.scroll_line is None else min(self.scroll_line, last)

    def view_key(self) -> tuple:
        """Changes whenever render_window() would return something different."""
        with self._lock:
            return self._version, self._target_line()

    def render_window(self, max_lines: int) -> Tuple[tuple, List[str]]:
        """(view key, up to max_lines CR-collapsed lines ending at the scroll target)."""
        with self._lock:
            target = self._target_line()
            start = max(0, target - max_lines + 1)
            return (self._version, target), [_collapse_cr(l) for l in self._lines[start:target + 1]]

    def scroll(self, delta: int):
        with self._lock:
            last = len(self._lines) - 1
            if last > 0 and self._lines[-1] == "":
                last -= 1
            cur = self._target_line()
            new = cur + delta
            self.scroll_line = None if new >= last else max(0, new)
        _REDRAW()

    def follow(self):
        with self._lock:
            self.scroll_line = None
        _REDRAW()

    @property
    def following(self) -> bool:
        return self.scroll_line is None


BUFFERS: Dict[str, OutputBuffer] = {
    "agent": OutputBuffer("agent", "AGENT"),
    "llm": OutputBuffer("llm", "LLM"),
    "stdout": OutputBuffer("stdout", "STDOUT"),
    "stderr": OutputBuffer("stderr", "STDERR"),
    "uvian": OutputBuffer("uvian", "UVIAN"),
    "mapreduce": OutputBuffer("mapreduce", "MAP-REDUCE"),
    "graph": OutputBuffer("graph", "GRAPH"),
    "iface": OutputBuffer("iface", "IFACE"),  # network interface pane (shown while joined)
}
RIGHT_TABS = ["llm", "stdout", "stderr", "uvian", "mapreduce", "graph"]


def log(text: str = "", buf: str = "agent"):
    BUFFERS[buf].writeln(text)


# ==========================================
# 3. UVIAN STORE (DIRECT TEXT SEARCH ON DISK)
# ==========================================
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "what", "how", "can", "you",
    "are", "was", "were", "has", "have", "had", "not", "but", "all", "any", "our", "your",
    "please", "show", "tell", "give", "make", "let", "get", "use", "using", "want", "need",
    "about", "there", "then", "than", "them", "they", "which", "when", "where", "who", "why",
}


class UvianStore:
    """Uvian entries live at <root>/<u[0]>/<u[1]>/<u[2:4]>/<uuid>. Search reads files directly."""

    def __init__(self, root: str, max_file_bytes: int = 512 * 1024):
        self.root = resolve_path(root)
        self.max_file_bytes = max_file_bytes
        self._cache: Dict[str, Tuple[float, int, str]] = {}
        self._lock = threading.Lock()

    def path_for(self, uid: str) -> str:
        return resolve_path(os.path.join(self.root, uid[0:1], uid[1:2], uid[2:4], uid))

    def exists(self, uid: Optional[str]) -> bool:
        return bool(uid) and len(uid) >= 4 and os.path.isfile(self.path_for(uid))

    def iter_uuids(self):
        if not os.path.isdir(self.root):
            return
        for dirpath, _dirs, files in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root).replace("\\", "/").split("/")
            if len(rel) != 3:
                continue
            d1, d2, d3 = rel
            for name in files:
                if len(name) >= 4 and name[0] == d1 and name[1] == d2 and name[2:4] == d3:
                    yield name

    def read_text(self, uid: str) -> str:
        path = self.path_for(uid)
        try:
            st = os.stat(path)
        except OSError:
            return ""
        with self._lock:
            cached = self._cache.get(uid)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                return cached[2]
        try:
            with open(path, "rb") as f:
                raw = f.read(self.max_file_bytes)
            text = "" if b"\x00" in raw[:2048] else raw.decode("utf-8", errors="ignore")
        except OSError:
            text = ""
        with self._lock:
            self._cache[uid] = (st.st_mtime, st.st_size, text)
        return text

    def describe(self, uid: str, width: int = 140) -> str:
        """One-line preview: first markdown heading, else first meaningful line."""
        text = self.read_text(uid)
        first = ""
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#!"):
                continue
            if re.match(r"^#{1,2}\s+", s):
                first = s
                break
            if not first:
                first = s
        return (first[: width - 1] + "…") if len(first) > width else first or "(empty)"

    @staticmethod
    def query_terms(query: str) -> List[str]:
        words = re.findall(r"[\w./:-]+", query.lower())
        terms = [w.strip(".:-/") for w in words]
        terms = [t for t in terms if t and (len(t) >= 3 or any(c.isdigit() for c in t)) and t not in STOPWORDS]
        if not terms:
            terms = [w for w in words if w]
        return list(dict.fromkeys(terms))

    def search(self, query: str, max_candidates: int = 12, max_lines: int = 12) -> List[dict]:
        terms = self.query_terms(query)
        if not terms:
            return []
        patterns = [re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE) for t in terms]
        results = []
        for uid in self.iter_uuids():
            text = self.read_text(uid)
            if not text:
                continue
            hit_terms = set()
            matched, shown = 0, []
            for line in text.splitlines():
                clean = line.strip()
                hits = [i for i, p in enumerate(patterns) if p.search(clean)]
                is_header = bool(re.match(r"^#{1,2}\s+", line))
                if hits:
                    matched += 1
                    hit_terms.update(hits)
                if (hits or is_header) and len(shown) < max_lines:
                    shown.append((clean[:200], bool(hits), is_header))
            if uid.lower() in query.lower():
                hit_terms.add(-1)
            if hit_terms:
                score = len(hit_terms) * 10 + min(matched, 20)
                results.append({"uuid": uid, "score": score, "lines": shown})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max_candidates]


# ==========================================
# 4. MEMGRAPH RELATION GRAPH
# ==========================================
class MemgraphLinker:
    """
    Tracks how Uvian entries relate. Nodes: (:Uvian {uuid}). Edges: [:RELATED {weight, kinds}]
    stored once per pair (canonical direction, queried undirected).
    Clusters come from MAGE community_detection when available (===c).
    """

    def __init__(self, uri: Optional[str], user: str = "", password: str = ""):
        self.uri = uri
        self.driver = None
        self.error = ""
        self._lock = threading.Lock()
        if not uri:
            self.error = "disabled"
            return
        if GraphDatabase is None:
            self.error = "neo4j driver missing (pip install neo4j)"
            return
        try:
            auth = (user, password) if user else None
            self.driver = GraphDatabase.driver(uri, auth=auth)
            self.driver.verify_connectivity()
            for q in ("CREATE INDEX ON :Uvian(uuid);", "CREATE CONSTRAINT ON (u:Uvian) ASSERT u.uuid IS UNIQUE;"):
                try:
                    self._run(q)
                except Exception:
                    pass  # already exists, or not supported by this server version
        except Exception as e:
            self.error = f"unreachable: {e}"
            self.driver = None

    @property
    def enabled(self) -> bool:
        return self.driver is not None

    def status(self) -> str:
        return f"memgraph {self.uri}" if self.enabled else f"memgraph off ({self.error})"

    def _run(self, query: str, **params) -> List[dict]:
        with self._lock:
            with self.driver.session() as session:
                return session.run(query, **params).data()

    def touch(self, uid: str, preview: str = ""):
        if not self.enabled or not uid:
            return
        try:
            self._run(
                "MERGE (n:Uvian {uuid: $u}) "
                "ON CREATE SET n.created = timestamp() "
                "SET n.last_used = timestamp(), n.uses = coalesce(n.uses, 0) + 1, n.preview = $p",
                u=uid, p=preview[:200],
            )
        except Exception as e:
            log(f"\033[31m[GRAPH ERROR]\033[0m touch {uid}: {e}", "graph")

    def link(self, a: str, b: str, kind: str, weight: float = 1.0):
        if not self.enabled or not a or not b or a == b:
            return
        x, y = sorted((a, b))
        try:
            self._run(
                "MERGE (x:Uvian {uuid: $x}) MERGE (y:Uvian {uuid: $y}) "
                "MERGE (x)-[r:RELATED]->(y) "
                "ON CREATE SET r.weight = $w, r.kinds = [$k], r.created = timestamp() "
                "ON MATCH SET r.weight = r.weight + $w, "
                "  r.kinds = CASE WHEN $k IN r.kinds THEN r.kinds ELSE r.kinds + [$k] END "
                "SET r.updated = timestamp()",
                x=x, y=y, w=float(weight), k=kind,
            )
            log(f"\033[38;5;141m[LINK]\033[0m {a} ⇄ {b}  +{weight} ({kind})", "graph")
        except Exception as e:
            log(f"\033[31m[GRAPH ERROR]\033[0m link {a}-{b}: {e}", "graph")

    def related(self, uid: str, limit: int = 8) -> List[dict]:
        """Neighbours ranked by: direct edge weight > 2-hop paths > same community."""
        if not self.enabled or not uid:
            return []
        out: Dict[str, dict] = {}
        try:
            for r in self._run(
                "MATCH (s:Uvian {uuid: $u})-[r:RELATED]-(n:Uvian) "
                "RETURN n.uuid AS uuid, r.weight AS weight, r.kinds AS kinds "
                "ORDER BY weight DESC LIMIT $lim",
                u=uid, lim=limit,
            ):
                out[r["uuid"]] = {"uuid": r["uuid"], "score": float(r["weight"] or 0),
                                  "why": f"direct link w={r['weight']:.1f} via {','.join(r['kinds'] or [])}"}
            for r in self._run(
                "MATCH (s:Uvian {uuid: $u})-[r1:RELATED]-(m:Uvian)-[r2:RELATED]-(n:Uvian) "
                "WHERE n.uuid <> $u "
                "RETURN n.uuid AS uuid, m.uuid AS via, r1.weight * r2.weight AS w "
                "ORDER BY w DESC LIMIT $lim",
                u=uid, lim=limit * 3,
            ):
                if r["uuid"] in out:
                    continue
                score = 0.5 * float(r["w"] or 0) ** 0.5
                out[r["uuid"]] = {"uuid": r["uuid"], "score": score, "why": f"2-hop via {r['via']}"}
            for r in self._run(
                "MATCH (s:Uvian {uuid: $u}), (n:Uvian) "
                "WHERE s.community IS NOT NULL AND n.community = s.community AND n.uuid <> $u "
                "RETURN n.uuid AS uuid, s.community AS c, coalesce(n.uses, 0) AS uses "
                "ORDER BY uses DESC LIMIT $lim",
                u=uid, lim=limit,
            ):
                if r["uuid"] not in out:
                    out[r["uuid"]] = {"uuid": r["uuid"], "score": 0.25, "why": f"same cluster #{r['c']}"}
        except Exception as e:
            log(f"\033[31m[GRAPH ERROR]\033[0m related {uid}: {e}", "graph")
        return sorted(out.values(), key=lambda d: d["score"], reverse=True)[:limit]

    def recluster(self) -> str:
        if not self.enabled:
            return f"Memgraph not available: {self.error}"
        try:
            rows = self._run(
                "CALL community_detection.get() YIELD node, community_id "
                "SET node.community = community_id "
                "RETURN count(node) AS n, count(DISTINCT community_id) AS c"
            )
            r = rows[0] if rows else {"n": 0, "c": 0}
            return f"Clustered {r['n']} entries into {r['c']} communities."
        except Exception as e:
            return f"Community detection failed (needs Memgraph MAGE): {e}"

    def close(self):
        if self.driver:
            try:
                self.driver.close()
            except Exception:
                pass


# ==========================================
# 5. LLM & MAP-REDUCE
# ==========================================
def aux_slot_body() -> dict:
    """With llamark on, keep helper calls (selector, map-reduce) off the slot holding the cached entry."""
    return {"id_slot": ARGS.aux_slot} if ARGS.llamark else {}


def find_bash() -> str:
    """The bash for EVERY subprocess (shell tool, uvian #! entries, llamark.sh). On Windows use Git's
    bash by full path: a bare "bash" picks WSL's System32\\bash.exe, which can't see C:/ paths."""
    if is_windows():
        git_bash_path = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.exists(git_bash_path):
            return git_bash_path
        log("⚠️ [WARNING] Standard Git Bash path not found. Falling back to system PATH.")
    return "bash"


def llamark_load(path: str, slot: int, phase: Callable[[str], None]) -> bool:
    """Ask llamark.sh to restore <sha256>.bin of `path` into `slot` (it builds the bin on a miss).
    Returns True when the slot now holds the file as the system-turn prefix."""
    script = resolve_path(ARGS.llamark_script)
    if not os.path.isfile(script):
        log(f"⚠️ llamark script not found: {script}")
        return False
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    model_dir = os.path.splitext(os.path.basename(MODEL_NAME))[0]
    bin_hint = os.path.join(os.path.dirname(script), model_dir, f"{sha}.bin")
    # Only a hint: with a remote llama-server the bins live on that host, so a miss here
    # doesn't mean a build — llamark.sh always tries the server-side restore first.
    phase("llamark restore" if os.path.isfile(bin_hint) else "llamark restore (builds cache on first use)")
    env = dict(os.environ, LLAMARK_URL=f"http://{ARGS.host}:{ARGS.port}")
    t0 = time.time()
    # stdin=DEVNULL: never hand the child our stdin — under memphia_nvim it is the nvim protocol
    # pipe, and on Windows/Git Bash the child blocks reading it (hang until the user hits Enter).
    timeout = float(os.environ.get("LLAMARK_TIMEOUT", "900"))  # a first-time build prefills the whole file
    bash = find_bash()
    try:
        proc = subprocess.run([bash, script, "load", sha, path.replace("\\", "/"), str(slot)], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"⚠️ llamark load timed out after {timeout:.0f}s (LLAMARK_TIMEOUT); falling back to normal load")
        return False
    except OSError as e:
        log(f"⚠️ llamark could not start {bash} ({e}); set LLAMARK_BASH to Git's bash.exe")
        return False
    msg = (proc.stderr or proc.stdout).strip().splitlines()
    if proc.returncode != 0:
        log(f"⚠️ llamark load failed ({msg[-1] if msg else proc.returncode}) [bash={bash} script={script}]"
            f"; falling back to normal load")
        return False
    log(f"⚡ [LLAMARK] {sha[:12]}… in slot {slot} ({time.time() - t0:.1f}s) {msg[-1] if msg else ''}")
    return True


def standard_llm_call(system_prompt: str, user_prompt: str) -> str:
    response = CLIENT.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.0,
        extra_body=aux_slot_body(),
    )
    text = response.choices[0].message.content or ""
    text = re.sub(r"<\|channel>thought.*?<channel\|>", "", text, flags=re.DOTALL)
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class Cancelled(Exception):
    pass


MAP_SYSTEM_PROMPT = """You summarize ONE chunk of a larger text (command output, log, source file or document).
A downstream agent will read only your summaries, never the original, so the summary must stand on its own.

Rules:
- Be faithful. Never invent content. If the chunk is noise (repeated lines, binary, progress bars), say so briefly.
- Copy identifiers verbatim: file paths, UUIDs, commands, function names, versions, numbers, URLs, error codes.
- Describe structure too (e.g. "a table of 40 packages", "a stack trace", "section 3 of a README").
- Cover the whole chunk, not only parts related to the intent; but call out what matters for the intent.

Output exactly this format:
GIST: <one sentence: what this chunk is>
SUMMARY:
<3-8 sentences covering the chunk content in order>
KEY FACTS:
- <fact with verbatim identifiers>
ERRORS/WARNINGS:
- <verbatim error or warning line, or "none">
RELEVANCE: <high|medium|low> - <how this chunk relates to the user intent>"""

MERGE_SYSTEM_PROMPT = """You merge consecutive chunk summaries of ONE large text into a single summary of that span.
Keep line ranges, verbatim identifiers and all errors. Drop duplicates. Never invent content.
Output exactly this format:
GIST: <one sentence for the whole span>
SUMMARY:
<4-10 sentences, in order, mentioning which line ranges contain what>
KEY FACTS:
- <fact>
ERRORS/WARNINGS:
- <verbatim error or warning, or "none">
RELEVANCE: <high|medium|low> - <relation to the user intent>"""

REDUCE_SYSTEM_PROMPT = """You turn ordered chunk summaries of ONE large text into a final briefing for an agent that must act on it.
Never invent content. Keep identifiers verbatim. Preserve line ranges so the agent can locate details.
Output exactly this format:
OVERVIEW: <2-4 sentences: what the whole text is and its overall shape>
ANSWER TO INTENT: <what the text tells us about the user intent; say explicitly if it says nothing>
SECTION MAP:
- lines <a>-<b>: <what that part contains>
KEY FACTS:
- <fact>
ERRORS/WARNINGS:
- <verbatim error or warning, or "none">"""


class MapReduceSummarizer:
    """
    Line-aware chunking with overlap, a structured summary per chunk (with a running
    gist for continuity), hierarchical merging when summaries are still too large,
    a final briefing, and a verbatim tail (errors usually live at the end).
    """

    def __init__(self, llm_call: Callable[[str, str], str], chunk_chars: int,
                 cancel_check: Callable[[], bool] = lambda: False,
                 overlap_lines: int = 3, tail_lines: int = 25, tail_chars: int = 2500):
        self.llm_call = llm_call
        self.chunk_chars = max(1024, chunk_chars)
        self.cancel_check = cancel_check
        self.overlap_lines = overlap_lines
        self.tail_lines = tail_lines
        self.tail_chars = tail_chars

    def needs_reduction(self, text: str) -> bool:
        return len(text) > self.chunk_chars

    def chunk(self, text: str) -> List[Tuple[int, int, str]]:
        """Returns (first_line, last_line, chunk_text); line numbers are 1-based and inclusive."""
        lines = text.split("\n")
        pieces: List[Tuple[int, str]] = []  # (line_no, segment) — very long lines are split
        for no, line in enumerate(lines, 1):
            if len(line) <= self.chunk_chars:
                pieces.append((no, line))
            else:
                step = self.chunk_chars - 200
                for i in range(0, len(line), step):
                    pieces.append((no, line[i:i + step]))
        chunks, start = [], 0
        while start < len(pieces):
            size, end = 0, start
            while end < len(pieces) and (size + len(pieces[end][1]) + 1 <= self.chunk_chars or end == start):
                size += len(pieces[end][1]) + 1
                end += 1
            body = "\n".join(p[1] for p in pieces[start:end])
            chunks.append((pieces[start][0], pieces[end - 1][0], body))
            if end >= len(pieces):
                break
            start = max(start + 1, end - self.overlap_lines)
        return chunks

    @staticmethod
    def _gist(summary: str) -> str:
        m = re.search(r"^GIST:\s*(.+)$", summary, re.MULTILINE)
        return m.group(1).strip() if m else summary.strip().split("\n")[0][:200]

    def _check(self):
        if self.cancel_check():
            raise Cancelled()

    def _verbatim_tail(self, text: str) -> str:
        tail = "\n".join(text.rstrip("\n").split("\n")[-self.tail_lines:])
        return tail[-self.tail_chars:]

    def run(self, intent: str, text: str, source: str) -> str:
        if not self.needs_reduction(text):
            return text
        mr = BUFFERS["mapreduce"]
        total_lines = text.count("\n") + 1
        chunks = self.chunk(text)
        log(f"\033[35m[MAP-REDUCE]\033[0m {source}: {len(text)} chars / {total_lines} lines -> {len(chunks)} chunks")
        mr.writeln(f"\033[1;35m══ MAP-REDUCE: {source} ══\033[0m")
        mr.writeln(f"{len(text)} chars, {total_lines} lines, {len(chunks)} chunks of ≤{self.chunk_chars} chars")

        summaries, prev_gist = [], "(this is the first chunk)"
        for i, (a, b, body) in enumerate(chunks, 1):
            self._check()
            mr.writeln(f"\033[35m── map chunk {i}/{len(chunks)}  lines {a}-{b} ──\033[0m")
            user = (
                f"User intent: {intent}\n"
                f"Source: {source}\n"
                f"Position: chunk {i} of {len(chunks)}, lines {a}-{b} of {total_lines}\n"
                f"Previous chunk gist: {prev_gist}\n\n"
                f"<chunk>\n{body}\n</chunk>"
            )
            try:
                summary = self.llm_call(MAP_SYSTEM_PROMPT, user).strip()
            except Exception as e:
                summary = f"GIST: (summary failed: {e})\nSUMMARY:\nFirst lines:\n" + "\n".join(body.split("\n")[:10])
            mr.writeln(summary)
            prev_gist = self._gist(summary)
            summaries.append(f"### Chunk {i}/{len(chunks)} (lines {a}-{b})\n{summary}")

        final = self._reduce(intent, summaries, source)
        mr.writeln("\033[1;35m── final briefing ──\033[0m")
        mr.writeln(final)
        return (
            f"[MAP-REDUCED {source}: {len(text)} chars, {total_lines} lines, {len(chunks)} chunks]\n"
            f"{final}\n\n[VERBATIM TAIL]\n{self._verbatim_tail(text)}"
        )

    def _reduce(self, intent: str, parts: List[str], source: str, depth: int = 0) -> str:
        self._check()
        joined = "\n\n".join(parts)
        if len(joined) > self.chunk_chars and len(parts) > 1 and depth < 5:
            groups, cur, size = [], [], 0
            for p in parts:
                if cur and size + len(p) > self.chunk_chars:
                    groups.append(cur)
                    cur, size = [], 0
                cur.append(p)
                size += len(p) + 2
            if cur:
                groups.append(cur)
            if len(groups) == len(parts):  # every summary alone is too big; pair them up
                groups = [parts[i:i + 2] for i in range(0, len(parts), 2)]
            BUFFERS["mapreduce"].writeln(f"\033[35m── merge level {depth + 1}: {len(parts)} -> {len(groups)} ──\033[0m")
            merged = []
            for gi, g in enumerate(groups, 1):
                self._check()
                if len(g) == 1:
                    merged.append(g[0])
                    continue
                ranges = re.findall(r"lines (\d+)-(\d+)", "\n".join(g))
                span = f"lines {ranges[0][0]}-{ranges[-1][1]}" if ranges else f"group {gi}"
                m = self.llm_call(MERGE_SYSTEM_PROMPT,
                                  f"User intent: {intent}\nSource: {source}\n\n" + "\n\n".join(g))
                merged.append(f"### Span {gi}/{len(groups)} ({span})\n{m.strip()}")
            return self._reduce(intent, merged, source, depth + 1)
        try:
            return self.llm_call(
                REDUCE_SYSTEM_PROMPT,
                f"User intent: {intent}\nSource: {source}\n\nOrdered chunk summaries:\n\n{joined}",
            ).strip()
        except Exception as e:
            return f"(final reduce failed: {e})\n\n{joined[: self.chunk_chars]}"


# ==========================================
# 6. PROCESS EXECUTION (STREAMED INTO BUFFERS)
# ==========================================
def run_process(cmd_args: List[str], cwd: Optional[str], timeout: int,
                out_buf: OutputBuffer, err_buf: OutputBuffer,
                cancel_event: threading.Event, executable: Optional[str] = None,
                err_prefix: str = "") -> Tuple[int, str, str]:
    kwargs = {}
    if not is_windows():
        kwargs["start_new_session"] = True  # own process group so we can kill children too
    proc = subprocess.Popen(
        cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=cwd, executable=executable, **kwargs,
    )
    out_parts: List[str] = []
    err_parts: List[str] = []

    def pump(pipe, sink, buf, prefix):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                buf.write(prefix + line if prefix else line)
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out_parts, out_buf, ""), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err_parts, err_buf, err_prefix), daemon=True),
    ]
    for t in threads:
        t.start()

    def kill():
        try:
            if not is_windows():
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass

    deadline = time.time() + timeout
    rc = None
    while rc is None:
        try:
            rc = proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                kill()
                proc.wait()
                err_parts.append("\n[cancelled by user]")
                rc = 130
            elif time.time() > deadline:
                kill()
                proc.wait()
                err_parts.append(f"\n[timed out after {timeout}s]")
                rc = 124
    for t in threads:
        t.join(timeout=2)
    return rc, "".join(out_parts).strip(), "".join(err_parts).strip()


def is_autopilot_enabled() -> bool:
    autopilot_path = resolve_path("~/echo-home/autopilot.yaml")
    if os.path.exists(autopilot_path):
        try:
            with open(autopilot_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            allow_val = (config.get("memphia") or {}).get("allow", False)
            if allow_val is True or str(allow_val).strip().lower() in ["yes", "true", "1", "y"]:
                return True
        except Exception as e:
            log(f"⚠️ [AUTOPILOT WARNING] Failed to read {autopilot_path}: {e}")
    return False


def get_os_env_context() -> str:
    return (
        f"\n\n[OS ENVIRONMENT CONTEXT]\n"
        f"- OS/Kernel: {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"- Current User: {os.environ.get('USER', os.environ.get('USERNAME', 'unknown'))}\n"
        f"- Working Directory: {to_bash_path(CURRENT_CWD)}\n"
        f"- Default Shell: {os.environ.get('SHELL', 'default/bash')}"
    )


def execute_shell_command(command: str, cancel_event: threading.Event) -> Dict:
    global CURRENT_CWD
    out_b, err_b = BUFFERS["stdout"], BUFFERS["stderr"]
    header = f"\033[38;5;112m$ {command}\033[0m   \033[90m[{to_bash_path(CURRENT_CWD)}]\033[0m"
    out_b.writeln(header)
    err_b.writeln(header)

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
        cwd_file = tf.name
    wrapped = f"trap 'pwd > \"{to_bash_path(cwd_file)}\"' EXIT\n{command}"
    try:
        rc, out, err = run_process([find_bash(), "-c", wrapped], CURRENT_CWD, ARGS.cmd_timeout,
                                   out_b, err_b, cancel_event)
        try:
            with open(cwd_file, "r", encoding="utf-8") as f:
                captured = f.read().strip()
            if captured:
                new_cwd = resolve_path(captured)
                if os.path.isdir(new_cwd) and new_cwd != CURRENT_CWD:
                    log(f"\033[33m[CWD CHANGED]\033[0m {to_bash_path(CURRENT_CWD)} -> {to_bash_path(new_cwd)}")
                    CURRENT_CWD = new_cwd
        except Exception:
            pass
        return {"exit_code": rc, "stdout": out, "stderr": err}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Error: {e}"}
    finally:
        try:
            os.unlink(cwd_file)
        except Exception:
            pass
        out_b.writeln(f"\033[90m── end ──\033[0m")
        err_b.writeln(f"\033[90m── end ──\033[0m")


def execute_uvian_entry(store: UvianStore, uid: str, cancel_event: threading.Event) -> Dict[str, str]:
    file_path = store.path_for(uid)
    if not os.path.exists(file_path):
        return {"type": "error", "content": f"Uvian target file not found at {file_path}"}
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
    except Exception as e:
        return {"type": "error", "content": f"Failed to inspect header: {e}"}

    if first_line.startswith("#!"):
        low = first_line.lower()
        if "python" in low:
            # Windows: "python3" is often the Microsoft Store stub; reuse the running interpreter.
            engine = sys.executable if is_windows() else ("python3" if shutil.which("python3") else "python")
        elif "node" in low:
            engine = "node"
        else:
            engine = find_bash()
    else:
        engine = "cat"
        if is_windows() and not shutil.which("cat"):
            content = store.read_text(uid)
            BUFFERS["uvian"].writeln(content)
            return {"type": "tool_execution", "uuid": uid, "engine": "read", "exit_code": "0",
                    "stdout": content.strip(), "stderr": ""}

    is_bash_engine = "bash" in engine.lower() or engine in ["sh", "cat"]
    exec_path = to_bash_path(file_path) if is_bash_engine else file_path
    ub = BUFFERS["uvian"]
    ub.writeln(f"\033[48;5;54m\033[38;5;177m ⚡ {uid} \033[0m via '{engine}'")
    log(f"⚡ [UVIAN] executing {uid} via '{engine}'")
    try:
        rc, out, err = run_process([engine, exec_path], None, ARGS.cmd_timeout, ub, ub,
                                   cancel_event, err_prefix="\033[31m[stderr]\033[0m ")
        ub.writeln(f"\033[90m── exit {rc} ──\033[0m")
        return {"type": "tool_execution", "uuid": uid, "engine": engine, "exit_code": str(rc),
                "stdout": out, "stderr": err}
    except Exception as e:
        return {"type": "error", "content": f"Execution failed: {e}"}


# ==========================================
# 7. AGENT CORE
# ==========================================
SYSTEM_PROMPT = """<|think|>
You are Memphia, an interactive shell operator with autonomous memory tool execution.
Your goal is to fulfill the User Intent, verify it worked using stdout/stderr proof, and report the final status.

RULES:
1. Operate in a loop: each turn either run a shell command, load a related Uvian memory entry, or declare the task finished.
2. Do not declare SUCCESS without explicit proof from stdout/stderr or provided Uvian facts.
3. [RELATED MEMORY SUGGESTIONS] lists Uvian entries linked to the selected entry in the knowledge graph.
   They are NOT loaded. Load one (action_type "load_uvian") only when its preview suggests it is needed for the intent.
4. Messages starting with "User Steering Update" come from the user mid-task. Follow them; they override earlier plans.
5. A task starting with "[Network request from @name]" was relayed by your network interface from another member of the
   Intelligence Network. Do the work as usual and put what the requester should learn in "answer"; the interface decides what is shared.
6. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_command" or "load_uvian" or "declare_result",
  "command": "<shell command> or null",
  "uuid": "<uuid to load> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<short explanation of this step or the result>",
  "answer": "<declare_result only: the findings/answer for whoever asked, or null>"
}
"""

SELECT_SYSTEM_PROMPT = (
    "You are a UUID selection assistant.\n"
    "Analyze the user's intent and the candidate memory UUID entries (YAML: matched lines and headings).\n"
    "Select the single UUID that is most relevant to fulfilling the user's intent.\n"
    "Also list other candidate UUIDs that are genuinely related to the selected one or to the intent "
    "(they will be suggested to the agent and linked in a knowledge graph). Do not list unrelated ones.\n"
    "If none of the candidates are relevant, set selected_uuid to null.\n"
    "Respond strictly in valid JSON:\n"
    '{\n  "selected_uuid": "<uuid or null>",\n  "related_uuids": ["<uuid>", ...],\n  "reason": "<short explanation>"\n}'
)

HELP_TEXT = """\033[1mMemphia commands\033[0m
  <text>            idle: start a task │ busy: steering message for the next turn
  !<text>           steer and interrupt the current LLM generation now
  +<text>           queue a new task to run after the current one
  ===l              pin the last used uuid          ====      unpin + clear all queues
  =-= <keywords>    search the uvian folder         ===p <uuid|#>  pin a uuid or search result
  ===g [uuid]       show graph neighbours           ===k <a> <b>   link two entries manually
  ===a <#|uuid>...  load GRAPH entry # (or a uuid) into the LLM context at the next turn
  ===a! <#|uuid>... same, interrupting the current LLM generation so it applies now
  ===c              recompute clusters (MAGE community detection)
  ===q              show queues                     ===x      cancel the running task
  ===n              network interface (iFace): ===nj join, ===nl leave, ===ns say, ===n help
  ===h              this help                       exit / quit"""


class MemphiaAgent:
    def __init__(self, store: UvianStore, graph: MemgraphLinker):
        self.store = store
        self.graph = graph
        self.lock = threading.RLock()
        self.tasks: deque = deque()
        self.steering: deque = deque()
        self.attachments: deque = deque()  # uuids the user asked to load into the LLM context
        self.wake = threading.Event()
        self.cancel_event = threading.Event()
        self.interrupt_event = threading.Event()
        self.running = True
        self.busy = False
        self.phase = "idle"
        self.current_task = ""
        self.pinned_uuid: Optional[str] = None
        self.last_uuid: Optional[str] = None
        self.approval: Optional[dict] = None
        self.last_search: List[dict] = []
        self.last_graph: List[str] = []  # numbered entries of the latest GRAPH pane listing
        self.llm_slot: Optional[int] = None  # llama-server slot pinned for the agent loop (llamark)
        self.last_result: Optional[dict] = None  # {intent, status, reason, answer, trail} of the running/last task
        self.task_listeners: List[Callable[[str, dict], None]] = []  # fn(task, result) after every task
        self.iface = None  # memphia_iface.IFace, set by bootstrap()
        self.mr =MapReduceSummarizer(standard_llm_call, ARGS.chunk_size,
                                      cancel_check=self.cancel_event.is_set)

    # ---------- queue API (called from the UI thread) ----------
    def submit_task(self, text: str):
        with self.lock:
            self.tasks.append(text)
        self.wake.set()
        _REDRAW()

    def submit_steer(self, text: str, interrupt: bool = False):
        with self.lock:
            self.steering.append(text)
        if interrupt:
            self.interrupt_event.set()
        _REDRAW()

    def attach(self, uid: str, interrupt: bool = False) -> bool:
        """Load a Uvian entry into the LLM context at the next turn (or the next task when idle).
        Returns False when it is already queued."""
        with self.lock:
            if uid in self.attachments:
                return False
            self.attachments.append(uid)
        if interrupt:
            self.interrupt_event.set()
        _REDRAW()
        return True

    def clear_queues(self) -> Tuple[int, int, int]:
        with self.lock:
            n = (len(self.steering), len(self.tasks), len(self.attachments))
            self.steering.clear()
            self.tasks.clear()
            self.attachments.clear()
        _REDRAW()
        return n

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.interrupt_event.set()
            log("\033[41;97m [CANCEL REQUESTED] \033[0m")

    def interrupt(self):
        if self.busy:
            self.interrupt_event.set()
            log("\033[33m⏹ Interrupt requested.\033[0m")

    def shutdown(self):
        self.running = False
        self.cancel()
        self.wake.set()

    def set_phase(self, phase: str):
        self.phase = phase
        _REDRAW()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    # ---------- approval (worker waits, UI answers) ----------
    def request_approval(self, kind: str, detail: str) -> Tuple[bool, str]:
        if is_autopilot_enabled():
            log("\033[33m⚡ [AUTOPILOT]\033[0m auto-approved via ~/echo-home/autopilot.yaml")
            return True, detail
        req = {"kind": kind, "detail": detail, "event": threading.Event(), "answer": None, "editing": False}
        with self.lock:
            self.approval = req
        log(f"\033[31m⚠️ [ACTION REQUIRED]\033[0m {kind}:\n  > \033[1m{detail}\033[0m\n"
            f"  answer in the input line: y = yes, n = no, e = edit")
        self.set_phase("waiting approval")
        try:
            while not req["event"].wait(0.1):
                if self.cancel_event.is_set():
                    req["answer"] = (False, detail)
                    break
        finally:
            with self.lock:
                self.approval = None
            _REDRAW()
        return req["answer"] or (False, detail)

    def answer_approval(self, text: str) -> bool:
        """Returns True when the input should be kept in the line (edit mode)."""
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
            req["event"].set()
        elif low in ("n", "no"):
            req["answer"] = (False, req["detail"])
            req["event"].set()
        elif low == "e" and req["kind"] == "shell command":
            req["editing"] = True
            return True
        elif low.startswith("e ") and req["kind"] == "shell command":
            req["answer"] = (True, t[2:].strip())
            req["event"].set()
        else:
            log("ℹ️ Pending approval: answer y, n or e. Other messages are queued as steering.")
            self.submit_steer(t)
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
            self.busy, self.current_task = True, task
            self.last_result = None
            outcome = None
            _REDRAW()
            try:
                found = self.run_task(task)
                if found and not self.pinned_uuid:
                    self.pinned_uuid = found
                    log(f"✅ Auto-pinned memory UUID {found} for the rest of this session. Type '====' to unpin.")
            except Cancelled:
                log("\033[33m🛑 Task cancelled.\033[0m")
                outcome = ("CANCELLED", "cancelled by the operator")
            except Exception as e:
                log(f"\033[31m❌ [AGENT ERROR]\033[0m {type(e).__name__}: {e}")
                outcome = ("ERROR", f"{type(e).__name__}: {e}")
            finally:
                result = self.last_result or {"intent": str(task), "status": "ERROR", "reason": "",
                                              "answer": None, "trail": []}
                if outcome:
                    result["status"], result["reason"] = outcome
                self.busy, self.current_task = False, ""
                self.set_phase("idle")
                for fn in list(self.task_listeners):
                    try:
                        fn(task, result)
                    except Exception as e:
                        log(f"\033[31m❌ task listener failed:\033[0m {type(e).__name__}: {e}")

    # ---------- memory selection ----------
    def select_uuid(self, intent: str) -> Tuple[Optional[str], List[str]]:
        self.set_phase("searching uvian")
        log("🔍 Searching Uvian folder by text...")
        cands = self.store.search(intent, max_candidates=ARGS.max_candidates)
        if not cands:
            log("🔍 Text matching found no candidates.")
            return None, []
        ub = BUFFERS["uvian"]
        ub.writeln(f"\033[1m📄 TEXT MATCH CANDIDATES for:\033[0m {intent[:120]}")
        yaml_lines = []
        for c in cands:
            yaml_lines.append(f"{c['uuid']}: |")
            ub.writeln(f"\033[48;5;54m\033[38;5;177m{c['uuid']}\033[0m \033[90m(score {c['score']})\033[0m")
            for text, is_match, is_header in c["lines"]:
                yaml_lines.append(f"    {text}")
                colour = "\033[38;5;146m" if is_header and not is_match else "\033[38;5;177m"
                ub.writeln(f"    {colour}{text}\033[0m")
        log(f"📄 {len(cands)} candidate(s) shown in the UVIAN pane. Asking LLM to select...")
        self._check_cancel()
        self.set_phase("selecting uuid")
        user = f"User Intent: {intent}\n\nCandidate Memory UUIDs (YAML):\n```yaml\n" + "\n".join(yaml_lines) + "\n```"
        try:
            resp = CLIENT.chat.completions.create(
                model=MODEL_NAME, temperature=0.0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SELECT_SYSTEM_PROMPT}, {"role": "user", "content": user}],
                extra_body=aux_slot_body(),
            )
            parsed = parse_llm_json(resp.choices[0].message.content or "") or {}
        except Exception as e:
            log(f"❌ Error during LLM UUID selection: {e}")
            return None, []
        valid = {c["uuid"] for c in cands}
        sel = parsed.get("selected_uuid")
        related = [u for u in (parsed.get("related_uuids") or []) if isinstance(u, str) and u in valid and u != sel]
        if sel in valid:
            log(f"🎯 LLM selected {sel} — {parsed.get('reason', '')}")
            if related:
                log(f"🔗 Also relevant: {', '.join(related)}")
            return sel, related
        log(f"ℹ️ No relevant UUID. ({parsed.get('reason', '')})")
        return None, []

    def collect_suggestions(self, anchor: Optional[str], extra: List[str], exclude: set) -> List[dict]:
        out: Dict[str, dict] = {}
        for u in extra:
            if u not in exclude:
                out[u] = {"uuid": u, "score": 1.0, "why": "co-matched in text search"}
        if anchor:
            for r in self.graph.related(anchor, limit=ARGS.related_limit):
                if r["uuid"] in exclude or not self.store.exists(r["uuid"]):
                    continue
                if r["uuid"] in out:
                    out[r["uuid"]]["why"] += f"; {r['why']}"
                    out[r["uuid"]]["score"] += r["score"]
                else:
                    out[r["uuid"]] = r
        ranked = sorted(out.values(), key=lambda d: d["score"], reverse=True)[: ARGS.related_limit]
        for r in ranked:
            r["preview"] = self.store.describe(r["uuid"])
        if ranked:
            self.show_graph_entries(f"🧭 Suggestions around {anchor or '(search)'}",
                                    [(r["uuid"], r["why"], r["preview"]) for r in ranked])
        return ranked

    def show_graph_entries(self, title: str, entries: List[Tuple[str, str, str]]):
        """Numbered GRAPH pane listing; `===a <#>` loads entry # into the LLM context."""
        with self.lock:
            self.last_graph = [e[0] for e in entries]
        gb = BUFFERS["graph"]
        gb.writeln(f"\033[1m{title}\033[0m  \033[90m(===a <#> loads into LLM context)\033[0m")
        for i, (uid, why, preview) in enumerate(entries, 1):
            gb.writeln(f"  \033[1m[{i}]\033[0m \033[38;5;177m{uid}\033[0m  {why}\n      {preview}")

    @staticmethod
    def format_suggestions(anchor: Optional[str], sugg: List[dict]) -> str:
        if not sugg:
            return ""
        lines = [f"\n\n[RELATED MEMORY SUGGESTIONS{' for ' + anchor if anchor else ''}]",
                 "Not loaded. Use action_type \"load_uvian\" with the uuid only if needed."]
        for s in sugg:
            lines.append(f"- {s['uuid']} ({s['why']}): {s['preview']}")
        return "\n".join(lines)

    def load_uvian_context(self, uid: str, intent: str) -> str:
        self.set_phase(f"running uvian {uid[:8]}")
        res = execute_uvian_entry(self.store, uid, self.cancel_event)
        self.last_uuid = uid
        self.graph.touch(uid, self.store.describe(uid))
        if res["type"] != "tool_execution":
            log(f"⚠️ Uvian error: {res['content']}")
            return f"\n\n[UVIAN ENTRY {uid}] ERROR: {res['content']}"
        self.set_phase("summarizing uvian output")
        out = self.mr.run(intent, res["stdout"], f"uvian {uid} stdout")
        err = self.mr.run(intent, res["stderr"], f"uvian {uid} stderr") if res["stderr"] else ""
        return (f"\n\n[UVIAN ENTRY {uid}] executed via '{res['engine']}', exit code {res['exit_code']}\n"
                f"Stdout: {out}\nStderr: {err}")

    def llamark_doc(self, uid: str) -> Optional[str]:
        """Return the raw text of a static (non-#!) entry after llamark put its KV cache in the
        agent slot, or None to use the normal execute/summarize path."""
        path = self.store.path_for(uid)
        try:
            with open(path, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if text.startswith("#!") or len(raw) > self.store.max_file_bytes:
            return None  # executable entries produce fresh output each run; not cacheable
        if not llamark_load(path, ARGS.llamark_slot, self.set_phase):
            return None
        ub = BUFFERS["uvian"]
        ub.writeln(f"\033[48;5;54m\033[38;5;177m ⚡ {uid} \033[0m via 'llamark' (KV cache, slot {ARGS.llamark_slot})")
        ub.writeln(text.rstrip("\n"))
        ub.writeln("\033[90m── end ──\033[0m")
        return text

    def load_into_context(self, uid: str, intent: str, target: Optional[str], loaded: List[str],
                          suggested_ids: set, kind: str) -> str:
        """Run/summarize uid, link it in the graph and return the message content for the LLM."""
        anchor = target or (loaded[-1] if loaded else None)
        ctx = self.load_uvian_context(uid, intent)
        if anchor:
            self.graph.link(anchor, uid, kind, 1.0)
        loaded.append(uid)
        more = [s for s in self.collect_suggestions(uid, [], set(loaded)) if s["uuid"] not in suggested_ids]
        suggested_ids.update(s["uuid"] for s in more)
        return ctx.strip() + self.format_suggestions(uid, more)

    def apply_attachments(self, intent: str, target: Optional[str], loaded: List[str],
                          suggested_ids: set, messages: List[dict]):
        with self.lock:
            uids = list(self.attachments)
            self.attachments.clear()
        for uid in uids:
            self._check_cancel()
            if uid in loaded:
                log(f"ℹ️ {uid} is already in the LLM context.")
                continue
            if not self.store.exists(uid):
                log(f"❌ Uvian entry {uid} no longer exists.")
                continue
            log(f"\033[38;5;177m📎 [MANUAL LOAD]\033[0m {uid}  {self.store.describe(uid)}")
            content = self.load_into_context(uid, intent, target, loaded, suggested_ids, "manual_load")
            messages.append({"role": "user", "content": "The user manually loaded this Uvian entry for you:\n" + content})

    # ---------- the agentic loop ----------
    def stream_llm(self, messages: List[dict]) -> Tuple[str, bool]:
        lb = BUFFERS["llm"]
        self.set_phase("thinking")
        extra = {"id_slot": self.llm_slot, "cache_prompt": True} if self.llm_slot is not None else {}
        response = CLIENT.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=0.0,
                                                  stream=True, extra_body=extra)
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
                        lb.write("\033[90m")
                        in_reasoning = True
                    lb.write(reasoning.replace("\n", "\n\033[90m"))
                if delta.content:
                    if in_reasoning:
                        lb.write("\033[0m\n")
                        in_reasoning = False
                    output += delta.content
                    lb.write(delta.content)
        finally:
            lb.write("\033[0m\n")
            if interrupted:
                try:
                    response.close()
                except Exception:
                    pass
        return output, interrupted

    def run_task(self, intent: str) -> Optional[str]:
        self.last_result = res = {"intent": str(intent), "status": "INCOMPLETE",
                                  "reason": "max turns reached", "answer": None, "trail": []}
        log(f"\n\033[1;36m▶ TASK:\033[0m {intent}")
        BUFFERS["llm"].writeln(f"\033[1;36m▶ TASK:\033[0m {intent}")
        target, related = None, []
        if self.pinned_uuid:
            target = self.pinned_uuid
            log(f"📌 Using pinned UUID {target}")
        else:
            target, related = self.select_uuid(intent)
        self._check_cancel()

        loaded: List[str] = []
        context = ""
        cached_doc = None  # target file text, when served from a llamark KV cache
        self.llm_slot = ARGS.llamark_slot if ARGS.llamark else None
        if target and ARGS.llamark:
            cached_doc = self.llamark_doc(target)
        if target:
            self.last_uuid = target
            if cached_doc is not None:
                self.graph.touch(target, self.store.describe(target))
                context += (f"\n\n[UVIAN ENTRY {target}] is loaded verbatim at the start of the system prompt "
                            f"(before the END MEMORY marker).")
            else:
                context += self.load_uvian_context(target, intent)
            loaded.append(target)
            for r in related:
                self.graph.link(target, r, "co_relevant", 0.5)
        suggested = self.collect_suggestions(target, related, set(loaded))
        suggested_ids = {s["uuid"] for s in suggested}
        if suggested:
            log(f"🧭 {len(suggested)} related entr{'y' if len(suggested) == 1 else 'ies'} suggested to the LLM (see GRAPH pane).")
        context += self.format_suggestions(target, suggested)

        # llamark: the entry must be the very first bytes of the system turn — that is the
        # prefix stored in <sha256>.bin, so the server reuses it instead of re-prefilling.
        system = SYSTEM_PROMPT if cached_doc is None else (
            f"{cached_doc}\n\n=== END MEMORY {target} ===\n\n{SYSTEM_PROMPT}")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"User Intent: {intent}{get_os_env_context()}{context}"},
        ]
        try:
            for turn in range(ARGS.max_turns):
                self._check_cancel()
                log(f"\033[36m\033[1m──── Turn {turn + 1} ────\033[0m")
                BUFFERS["llm"].writeln(f"\033[36m── turn {turn + 1} ──\033[0m")
                with self.lock:
                    steers = list(self.steering)
                    self.steering.clear()
                for s in steers:
                    log(f"\033[35m[STEERED]\033[0m {s}")
                    messages.append({"role": "user", "content": f"User Steering Update: {s}"})
                self.apply_attachments(intent, target, loaded, suggested_ids, messages)
                self.interrupt_event.clear()

                raw, interrupted = self.stream_llm(messages)
                self._check_cancel()
                if interrupted:
                    log("\033[41;97m [LLM INTERRUPTED] \033[0m steering will be applied now")
                    messages.append({"role": "user", "content": "System: The user interrupted this generation turn."})
                    continue

                action = parse_llm_json(raw)
                if action is None:
                    log("\033[31m⚠️ LLM output was not valid JSON; asking it to retry.\033[0m")
                    messages.append({"role": "assistant", "content": raw[-2000:]})
                    messages.append({"role": "user", "content": "System: Your last reply was not valid JSON. Reply with ONLY the JSON object per the schema."})
                    continue
                messages.append({"role": "assistant", "content": json.dumps(action)})
                kind = action.get("action_type")
                if action.get("reason"):
                    log(f"\033[90m💭 {action['reason']}\033[0m")
                if kind != "declare_result":
                    res["trail"].append(f"{kind}: {action.get('command') or action.get('uuid') or ''} — {action.get('reason') or ''}"[:400])

                if kind == "declare_result":
                    status = action.get("status")
                    colour = "\033[32m" if status == "SUCCESS" else "\033[31m"
                    log(f"{colour}❇  [FINAL RESULT] {status}\033[0m\n🧠 [REASON] {action.get('reason')}")
                    if action.get("answer"):
                        log(f"📝 [ANSWER] {action['answer']}")
                    res.update(status=status or "UNKNOWN", reason=action.get("reason") or "", answer=action.get("answer"))
                    return target

                if kind == "run_command":
                    command = action.get("command") or ""
                    ok, command = self.request_approval("shell command", command)
                    if not ok:
                        log("🚫 Command denied.")
                        messages.append({"role": "user", "content": f"Execution DENIED by user for command: {command}."})
                        continue
                    self.set_phase("running command")
                    log(f"\033[38;5;112m$ {command}\033[0m  (live output in STDOUT/STDERR panes)")
                    out_res = execute_shell_command(command, self.cancel_event)
                    self._check_cancel()
                    log(f"\033[32m[EXIT CODE]\033[0m {out_res['exit_code']}  stdout {len(out_res['stdout'])} chars, stderr {len(out_res['stderr'])} chars")
                    res["trail"].append(f"  ran `{command[:200]}` → exit {out_res['exit_code']}")
                    self.set_phase("summarizing output")
                    out = self.mr.run(intent, out_res["stdout"], f"stdout of `{command[:80]}`")
                    err = self.mr.run(intent, out_res["stderr"], f"stderr of `{command[:80]}`")
                    messages.append({"role": "user", "content": (
                        f"Command Executed: {command}\n"
                        f"Current Working Directory: {to_bash_path(CURRENT_CWD)}\n"
                        f"Exit Code: {out_res['exit_code']}\nstdout: {out}\nstderr: {err}")})
                    continue

                if kind == "load_uvian":
                    uid = (action.get("uuid") or "").strip()
                    if not self.store.exists(uid):
                        messages.append({"role": "user", "content": f"System: Uvian entry '{uid}' does not exist. Choose from the suggestions."})
                        continue
                    if uid in loaded:
                        messages.append({"role": "user", "content": f"System: Uvian entry {uid} is already loaded above."})
                        continue
                    ok, _ = self.request_approval("load uvian entry", f"{uid}  {self.store.describe(uid)}")
                    if not ok:
                        log("🚫 Uvian load denied.")
                        messages.append({"role": "user", "content": f"Loading Uvian entry {uid} was DENIED by user."})
                        continue
                    content = self.load_into_context(uid, intent, target, loaded, suggested_ids, "co_loaded")
                    messages.append({"role": "user", "content": content})
                    continue

                log(f"\033[31m❌ Unknown action type: {kind}\033[0m")
                messages.append({"role": "user", "content": f"System: Unknown action_type '{kind}'. Use run_command, load_uvian or declare_result."})

            log("⚠️ [WARNING] Max turns reached. Loop terminated.")
            return target
        finally:
            # Entries used together in one task belong to the same working cluster.
            for i in range(len(loaded)):
                for j in range(i + 1, len(loaded)):
                    if loaded[i] != target:
                        self.graph.link(loaded[i], loaded[j], "co_task", 0.5)



# ==========================================
# 8. INPUT ROUTING & COMMANDS (shared by all frontends)
# ==========================================
class CommandRouter:
    """Turns submitted input lines into agent actions. UI effects go through focus_tab."""

    def __init__(self, agent: MemphiaAgent, focus_tab: Callable[[str], None], keys_help: str = ""):
        self.agent, self.focus_tab, self.keys_help = agent, focus_tab, keys_help

    def accept(self, text: str) -> Tuple[str, str]:
        """Returns ("keep", text to leave in the input line), ("exit", "") or ("ok", "")."""
        clean = text.strip()
        a = self.agent
        if a.approval:
            if a.answer_approval(text):
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
                log(f"❌ Command error: {e}")
            return "ok", ""
        if a.busy:
            if clean.startswith("+"):
                a.submit_task(clean[1:].strip())
                log(f"\033[38;5;39m[QUEUED TASK]\033[0m {clean[1:].strip()}")
            elif clean.startswith("!"):
                a.submit_steer(clean[1:].strip(), interrupt=True)
                log(f"\033[35m[STEER+INTERRUPT]\033[0m {clean[1:].strip()}")
            else:
                a.submit_steer(clean)
                log(f"\033[35m[QUEUED STEER]\033[0m {clean}")
        else:
            a.submit_task(clean.lstrip("+!").strip())
        return "ok", ""

    def command(self, cmd: str):
        a = self.agent
        if cmd == "===h":
            log(HELP_TEXT + ("\n" + self.keys_help if self.keys_help else ""))
        elif cmd == "===l":
            if a.last_uuid:
                a.pinned_uuid = a.last_uuid
                log(f"📌 Memory pinned at {a.last_uuid}")
            else:
                log("ℹ️ No last uuid is found to be pinned.")
        elif cmd == "====":
            if a.pinned_uuid:
                log(f"🔓 Unpinned memory ({a.pinned_uuid}).")
                a.pinned_uuid = None
            else:
                log("ℹ️ No memory file is currently pinned.")
            s, t, n = a.clear_queues()
            log(f"🧹 Cleared {s} steering message(s), {t} queued task(s) and {n} pending load(s).")
        elif cmd == "===x":
            a.cancel() if a.busy else log("ℹ️ Nothing is running.")
        elif cmd == "===q":
            with a.lock:
                log(f"Queues: {len(a.steering)} steering, {len(a.tasks)} tasks, {len(a.attachments)} loads")
                for s in a.steering:
                    log(f"  STEER {s}")
                for t in a.tasks:
                    log(f"  TASK  {t}")
                for u in a.attachments:
                    log(f"  LOAD  {u}")
        elif cmd.startswith("=-="):
            q = cmd[3:].strip()
            if not q:
                log("⚠️ Please enter keywords after '=-='")
                return
            threading.Thread(target=self._search, args=(q,), daemon=True).start()
        elif cmd.startswith("===p"):
            arg = cmd[4:].strip()
            uid = arg
            if arg.isdigit() and 1 <= int(arg) <= len(a.last_search):
                uid = a.last_search[int(arg) - 1]["uuid"]
            if a.store.exists(uid):
                a.pinned_uuid = uid
                log(f"✅ Memory {uid} pinned! Used for subsequent prompts until '===='.")
            else:
                log(f"❌ UUID '{arg}' not found.")
        elif cmd.startswith("===a"):
            interrupt = cmd.startswith("===a!")
            args = cmd[5 if interrupt else 4:].split()
            if not args:
                log("ℹ️ Usage: ===a <#|uuid> ... (# = number in the GRAPH pane); ===a! also interrupts the LLM")
                return
            with a.lock:
                listing = list(a.last_graph)
            for arg in args:
                ref = arg.lstrip("#")
                uid = listing[int(ref) - 1] if ref.isdigit() and 1 <= int(ref) <= len(listing) else arg
                if not a.store.exists(uid):
                    log(f"❌ '{arg}' is not a GRAPH entry number or an existing uuid.")
                    continue
                if not a.attach(uid, interrupt=interrupt and a.busy):
                    continue
                when = "now" if interrupt and a.busy else "at the next turn" if a.busy else "into the next task"
                log(f"\033[38;5;177m📎 [LOAD QUEUED]\033[0m {uid} → LLM context {when}")
        elif cmd.startswith("===g"):
            uid = cmd[4:].strip() or a.pinned_uuid or a.last_uuid
            if not uid:
                log("ℹ️ Give a uuid: ===g <uuid>")
                return
            threading.Thread(target=self._show_graph, args=(uid,), daemon=True).start()
        elif cmd.startswith("===k"):
            parts = cmd[4:].split()
            if len(parts) != 2 or not all(a.store.exists(p) for p in parts):
                log("⚠️ Usage: ===k <uuid-a> <uuid-b> (both must exist in the uvian folder)")
                return
            threading.Thread(target=a.graph.link, args=(parts[0], parts[1], "manual", 2.0), daemon=True).start()
            log(f"🔗 Linked {parts[0]} ⇄ {parts[1]} (manual)")
        elif cmd.startswith("===n"):
            if a.iface is None:
                log("ℹ️ iFace is not available.")
            else:
                a.iface.command(cmd[4:])
        elif cmd == "===c":
            def run():
                log(f"🧩 {a.graph.recluster()}")
            threading.Thread(target=run, daemon=True).start()
        else:
            log(f"❓ Unknown command {cmd}. Type ===h for help.")

    def _search(self, q: str):
        a = self.agent
        res = a.store.search(q, max_candidates=40)
        a.last_search = res
        ub = BUFFERS["uvian"]
        self.focus_tab("uvian")
        ub.writeln(f"\033[1m🔎 search:\033[0m {q}")
        if not res:
            ub.writeln("No matches in the uvian folder.")
            log("🔍 No matches found.")
            return
        for i, r in enumerate(res, 1):
            ub.writeln(f"\033[44m\033[97m #{i} {r['uuid']} \033[0m \033[90mscore {r['score']}\033[0m")
            for text, _m, _h in r["lines"]:
                ub.writeln(f"    {text}")
        log(f"🔍 {len(res)} match(es) in the UVIAN pane. Pin with ===p <#|uuid>.")

    def _show_graph(self, uid: str):
        self.focus_tab("graph")
        a = self.agent
        rel = a.graph.related(uid, limit=20)
        if not rel:
            BUFFERS["graph"].writeln(f"\033[1m🧭 neighbours of {uid}\033[0m  ({a.graph.status()})\n"
                                     "  (no related entries recorded)")
            return
        a.show_graph_entries(f"🧭 neighbours of {uid}  ({a.graph.status()})",
                             [(r["uuid"], f"score {r['score']:.2f}  {r['why']}", a.store.describe(r["uuid"]))
                              for r in rel])


# ==========================================
# 9. BOOTSTRAP
# ==========================================
def get_model_name(host: str, port: int) -> Optional[str]:
    try:
        response = requests.get(f"http://{host}:{port}/models", timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("models") and "name" in data["models"][0]:
            return data["models"][0]["name"]
        if data.get("data") and "id" in data["data"][0]:
            return data["data"][0]["id"]
    except Exception as e:
        print(f"\n❌ ERROR connecting to LLM: {e}", file=sys.stderr)
    return None


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Memphia: interactive LLM agent with Uvian memory and a Memgraph relation graph.")
    p.add_argument("--port", type=int, default=8080, help="Port of the local LLM server")
    p.add_argument("--host", type=str, default="localhost", help="Hostname of the local LLM server")
    p.add_argument("--chunk-size", type=int, default=32768, help="Max characters before Map-Reduce is triggered (also the chunk size)")
    p.add_argument("--uvian-dir", type=str, default="~/echo-home/memory/uvian", help="Root directory for uvian storage")
    p.add_argument("--memgraph-uri", type=str, default=os.environ.get("MEMGRAPH_URI", "bolt://localhost:7687"),
                   help="Bolt URI of the Memgraph endpoint (env MEMGRAPH_URI)")
    p.add_argument("--memgraph-user", type=str, default=os.environ.get("MEMGRAPH_USER", ""))
    p.add_argument("--memgraph-password", type=str, default=os.environ.get("MEMGRAPH_PASSWORD", ""))
    p.add_argument("--no-memgraph", action="store_true", help="Disable the relation graph")
    p.add_argument("--related-limit", type=int, default=8, help="Max related entries suggested to the LLM")
    p.add_argument("--max-candidates", type=int, default=12, help="Max text-search candidates sent to the selector LLM")
    p.add_argument("--llamark", action="store_true",
                   help="Serve the selected uvian entry from a saved KV cache (<sha256>.bin) via llamark.sh")
    p.add_argument("--llamark-script", type=str, default="~/echo-home/memory/llamark/llamark.sh")
    p.add_argument("--llamark-slot", type=int, default=0, help="llama-server slot for the agent loop + cache")
    p.add_argument("--aux-slot", type=int, default=1, help="slot for selector/map-reduce calls when --llamark is on")
    p.add_argument("--max-turns", type=int, default=13)
    p.add_argument("--cmd-timeout", type=int, default=30, help="Seconds before a shell/uvian command is killed")
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", type=str, default=os.environ.get("MEMPHIA_NET_HUB", "127.0.0.1:7788"),
                   help="host:port of the network (inet/inet_hub.py; env MEMPHIA_NET_HUB)")
    g.add_argument("--net-network", type=str, default="", help="name of the network (shown until the hub announces its own)")
    g.add_argument("--net-name", type=str, default="", help="join the network at startup under this name")
    g.add_argument("--net-purpose", type=str, default="", help="why this agent is on the network (steers what it answers)")
    g.add_argument("--net-max-depth", type=int, default=4, help="ignore agent messages deeper than this in a reply chain")
    g.add_argument("--net-rate", type=int, default=6, help="max own messages per minute before ignoring other agents")
    g.add_argument("--net-no-autoshare", action="store_true", help="never consider sharing results of local tasks")
    return p.parse_args(argv)


def bootstrap(argv=None) -> Optional[MemphiaAgent]:
    """Parse args, connect to the LLM and build the agent. Returns None when the LLM is unreachable."""
    global ARGS, CLIENT, MODEL_NAME, CURRENT_CWD
    ARGS = parse_args(argv)
    ARGS.uvian_dir = resolve_path(ARGS.uvian_dir)
    CURRENT_CWD = resolve_path(os.getcwd())
    CLIENT = OpenAI(base_url=f"http://{ARGS.host}:{ARGS.port}/v1", api_key="localm")
    MODEL_NAME = get_model_name(ARGS.host, ARGS.port)
    if not MODEL_NAME:
        return None
    store = UvianStore(ARGS.uvian_dir)
    graph = MemgraphLinker(None if ARGS.no_memgraph else ARGS.memgraph_uri,
                           ARGS.memgraph_user, ARGS.memgraph_password)
    agent = MemphiaAgent(store, graph)
    import memphia_iface  # late import: it imports this module
    agent.iface = memphia_iface.IFace(agent)
    return agent


def announce(agent: MemphiaAgent, quit_hint: str):
    n_entries = sum(1 for _ in agent.store.iter_uuids())
    log(f"✅ LLM Ready: {MODEL_NAME}")
    log(f"🧠 Uvian folder: {agent.store.root} ({n_entries} entries, searched directly)")
    log(f"🕸  {agent.graph.status()}")
    log(f"🖥️  {get_os_env_context().strip()}")
    log(f"🚀 Memphia ready. Type a task; type while it runs to steer. ===h for help, {quit_hint} to quit.")
    if ARGS.net_name:
        agent.iface.join()
    else:
        log(f"🌐 Intelligence Network: ===nj <name> [purpose] to plug in the iFace (hub {ARGS.net_hub}).")
