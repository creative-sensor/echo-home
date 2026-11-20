#!/usr/bin/env python
"""
hbmx_core — the large-file engine of hbmx (no LLM).

hbmx never loads a large file into an LLM context. This module identifies a file (libmagic `file`, its own
signature table, text-format detectors), maps it in ONE streaming pass (sha256, line index, a grid of cells
with density stats) and keeps everything it learns in the file's memory:

  $HBMX_HOME (default ~/echo-home/memory/hbmx)/<sha[0]>/<sha[1]>/<sha[2:4]>/<full sha256>/   the file's hbmx_path
      meta.yaml              the map: identity, structure, regions (index tree), zones, landmarks, functions,
                             howto — the file's "world map", read first on every later visit
      knowledge.yaml         what analyses established: key findings + every question answered (hbmx_know.py)
      .function/             helper scripts made for this file (templates at probe time + scripts the agent writes)
      .data/                 derived data: grid.json (line index + cell stats), toc.json, extract/, inflated copies
  $HBMX_HOME/catalog.yaml    every file ever read: path -> sha256, size, mtime, format, last read

A bucket folder (<d1>/<d2>/<d3d4>) holds one <full sha256>/ folder per file whose sha starts with those digits.

Queries live in hbmx_ops.py. Both serve hbmx.py (the agent), the helper scripts and the shell:
  hbmx_core.py probe <file> [--force]       identify + map (instant when the file is known), prints the brief
  hbmx_core.py brief|where|map <file> [--zone z3 | --region r1.2]
  hbmx_core.py files                        the catalog
  hbmx_core.py view|record|seek|search|filter|stats|extract|functions|run|note ...   (hbmx_ops.py, -h for each)
A file argument may be @<sha prefix> for a file already in memory.
"""
import bisect
import calendar
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML; without it the memory files are written as JSON (valid YAML)
except ImportError:  # pragma: no cover
    yaml = None
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

VERSION = 1
HOME = os.path.abspath(os.path.expanduser(os.environ.get("HBMX_HOME") or "~/echo-home/memory/hbmx"))
CORE = os.path.abspath(__file__)
PYTHON = sys.executable or "python3"
CHUNK = 8 << 20          # read size of the mapping pass
HEAD = 1024              # bytes kept of the first line of each cell (timestamps)
MAX_ZONES = 48           # zones shown on one map level
EXACT_AUTO = 256 << 20   # stats/scans read every line up to this many bytes, else sample (exact on request)


class Cancelled(Exception):
    pass


class HbmxError(Exception):
    pass


class Ctl:
    """Progress + cancellation for long passes. progress(phase, fraction) is rate-limited."""

    def __init__(self, progress: Optional[Callable[[str, float], None]] = None, cancel=None):
        self.progress, self.cancel, self._last = progress, cancel, 0.0

    def tick(self, phase: str, frac: float = 0.0, force: bool = False):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        now = time.time()
        if self.progress and (force or now - self._last > 0.25):
            self._last = now
            try:
                self.progress(phase, max(0.0, min(1.0, frac)))
            except Exception:
                pass


NOCTL = Ctl()


# ---------------------------------------------------------------- small helpers
def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def short_num(n) -> str:
    n = int(n or 0)
    if n >= 10_000_000:
        return f"{n / 1e6:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    return str(n)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ("Z" if ts == int(ts) else f".{int((ts % 1) * 1000):03d}Z")


def dur(sec: float) -> str:
    sec = abs(int(sec))
    if sec < 120:
        return f"{sec}s"
    if sec < 7200:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 172800:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d{sec % 86400 // 3600:02d}h"


def clip(s, n: int = 200) -> str:
    s = " ".join(str(s if s is not None else "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def show_line(b: bytes, width: int = 240) -> str:
    """One line of the file as safe, bounded text: decoded utf-8 (replacement chars), control chars escaped,
    tabs as ␉, clipped to width with the clipped length stated."""
    s = b.rstrip(b"\r\n").decode("utf-8", "replace")
    s = _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", s).replace("\t", "␉")
    if len(s) > width:
        s = s[:width] + f" …[+{len(s) - width} chars]"
    return s


def bar(frac: float, width: int = 8) -> str:
    blocks = " ▏▎▍▌▋▊▉█"
    frac = max(0.0, min(1.0, frac))
    full = int(frac * width)
    rest = int((frac * width - full) * 8)
    return ("█" * full + (blocks[rest] if full < width and rest else "")).ljust(width)


SPARK = "▁▂▃▄▅▆▇█"


def spark(v: float, vmax: float) -> str:
    if vmax <= 0 or v <= 0:
        return " "
    return SPARK[min(7, int(v / vmax * 7.999))]


# ---------------------------------------------------------------- memory layout
def bucket_dir(sha: str) -> str:
    return os.path.join(HOME, sha[0], sha[1], sha[2:4])


def entry_dir(sha: str) -> str:
    """The file's hbmx_path: everything hbmx keeps about one content."""
    return os.path.join(bucket_dir(sha), sha)


def meta_path(sha: str) -> str:
    return os.path.join(entry_dir(sha), "meta.yaml")


def func_dir(sha: str) -> str:
    return os.path.join(entry_dir(sha), ".function")


def data_dir(sha: str) -> str:
    return os.path.join(entry_dir(sha), ".data")


def ydump(obj) -> str:
    if yaml is None:
        return json.dumps(obj, indent=1, ensure_ascii=False)

    class _D(yaml.SafeDumper):
        pass

    def _str(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _D.add_representer(str, _str)
    return yaml.dump(obj, Dumper=_D, sort_keys=False, allow_unicode=True, width=160, default_flow_style=None)


def yload(text: str):
    if yaml is None:
        return json.loads(text)
    return yaml.safe_load(text)


def write_atomic(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"  # several threads save (worker, navigator, input)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class _Lock:
    """Advisory lock on a file in HOME (several hbmx processes may share the memory)."""

    def __init__(self, name: str):
        self.path = os.path.join(HOME, name)
        self.f = None

    def __enter__(self):
        os.makedirs(HOME, exist_ok=True)
        self.f = open(self.path, "a+")
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl and self.f:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        if self.f:
            self.f.close()


CATALOG = os.path.join(HOME, "catalog.yaml")


def catalog_load() -> dict:
    try:
        with open(CATALOG, encoding="utf-8") as f:
            cat = yload(f.read()) or {}
    except (OSError, ValueError):
        cat = {}
    if not isinstance(cat.get("files"), dict):
        cat = {"hbmx_catalog": VERSION, "files": {}}
    return cat


def catalog_put(path: str, rec: dict):
    with _Lock(".catalog.lock"):
        cat = catalog_load()
        cat["files"][path] = rec
        write_atomic(CATALOG, ydump(cat))


def ensure_home():
    """The memory root: README (layout) and .function/hbmx (shell entry to this engine)."""
    readme, entry = os.path.join(HOME, "README.md"), os.path.join(HOME, ".function", "hbmx")
    if os.path.isfile(readme) and os.path.isfile(entry):
        return
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    src = os.path.join(os.path.dirname(CORE), "memory_README.md")
    if not os.path.isfile(readme) and os.path.isfile(src):
        shutil.copyfile(src, readme)
    if not os.path.isfile(entry):
        write_atomic(entry, "#!/usr/bin/env bash\n# hbmx engine (no LLM): hbmx probe|map|view|seek|search|filter|stats|extract|"
                            "functions|run|note|files <file|@sha> ...  (-h for help)\n"
                            f'exec "${{HBMX_PYTHON:-{PYTHON}}}" "{CORE}" "$@"\n')
        os.chmod(entry, 0o755)


# ---------------------------------------------------------------- signatures (the probe's first look)
SIGS = [  # name, offset, magic, family
    ("gzip", 0, b"\x1f\x8b", "compressed"),
    ("zstd", 0, b"\x28\xb5\x2f\xfd", "compressed"),
    ("xz", 0, b"\xfd7zXZ\x00", "compressed"),
    ("bzip2", 0, b"BZh", "compressed"),
    ("lz4", 0, b"\x04\x22\x4d\x18", "compressed"),
    ("zip", 0, b"PK\x03\x04", "archive"),
    ("zip", 0, b"PK\x05\x06", "archive"),
    ("7z", 0, b"7z\xbc\xaf\x27\x1c", "archive"),
    ("tar", 257, b"ustar", "archive"),
    ("sqlite", 0, b"SQLite format 3\x00", "database"),
    ("parquet", 0, b"PAR1", "columnar"),
    ("arrow", 0, b"ARROW1", "columnar"),
    ("avro", 0, b"Obj\x01", "columnar"),
    ("hdf5", 0, b"\x89HDF\r\n\x1a\n", "scientific"),
    ("gguf", 0, b"GGUF", "model"),
    ("pcap", 0, b"\xd4\xc3\xb2\xa1", "capture"),
    ("pcap", 0, b"\xa1\xb2\xc3\xd4", "capture"),
    ("pcap", 0, b"\x4d\x3c\xb2\xa1", "capture"),
    ("pcap", 0, b"\xa1\xb2\x3c\x4d", "capture"),
    ("pcapng", 0, b"\x0a\x0d\x0d\x0a", "capture"),
    ("evtx", 0, b"ElfFile\x00", "log"),
    ("elf", 0, b"\x7fELF", "executable"),
    ("macho", 0, b"\xcf\xfa\xed\xfe", "executable"),
    ("macho", 0, b"\xce\xfa\xed\xfe", "executable"),
    ("macho-fat", 0, b"\xca\xfe\xba\xbe", "executable"),
    ("pe", 0, b"MZ", "executable"),
    ("pdf", 0, b"%PDF-", "document"),
    ("png", 0, b"\x89PNG\r\n\x1a\n", "image"),
    ("jpeg", 0, b"\xff\xd8\xff", "image"),
    ("gif", 0, b"GIF8", "image"),
]
# Magic numbers looked for INSIDE unknown binaries (embedded objects become landmarks, binwalk-lite).
EMBED_SIGS = [
    ("gzip", b"\x1f\x8b\x08"), ("zip", b"PK\x03\x04"), ("png", b"\x89PNG\r\n\x1a\n"), ("jpeg", b"\xff\xd8\xff\xe0"),
    ("jpeg", b"\xff\xd8\xff\xe1"), ("pdf", b"%PDF-"), ("elf", b"\x7fELF"), ("sqlite", b"SQLite format 3\x00"),
    ("7z", b"7z\xbc\xaf\x27\x1c"), ("xz", b"\xfd7zXZ\x00"), ("zstd", b"\x28\xb5\x2f\xfd"), ("bzip2", b"BZh91AY&SY"),
    ("pem", b"-----BEGIN "), ("ssh-key", b"openssh-key-v1"), ("tar", b"ustar\x00"), ("tar", b"ustar  \x00"),
]
MAX_SIG_HITS = 400


def match_sig(head: bytes) -> Optional[Tuple[str, str]]:
    for name, off, magic, family in SIGS:
        if head[off:off + len(magic)] == magic:
            if name == "pe" and b"PE\x00\x00" not in head[:1024]:
                continue
            if name == "bzip2" and not (len(head) > 3 and head[3:4] in b"123456789"):
                continue
            return name, family
    if len(head) > 10:  # safetensors: u64 header length, then a JSON object
        n = struct.unpack("<Q", head[:8])[0]
        if 2 <= n < 200 << 20 and head[8:10] in (b'{"', b"{ "):
            return "safetensors", "model"
    return None


def libmagic(path: str) -> Dict[str, str]:
    """The environment's signature tool: libmagic through `file`. HBMX_PROBE_TOOL adds another one (a command line,
    the path is appended; e.g. "trid", "exiftool -s -s -s -FileType", "binwalk --signature"): its output is kept
    in the identity as probe_tool."""
    out = {}
    if not shutil.which("file"):
        out["desc"] = "(file command not found)"
    else:
        for key, args in (("desc", ["-b"]), ("mime", ["-b", "--mime-type", "--mime-encoding"])):
            try:
                r = subprocess.run(["file", *args, path], capture_output=True, text=True, timeout=20)
                out[key] = r.stdout.strip()
            except (OSError, subprocess.SubprocessError) as e:
                out[key] = f"(file failed: {e})"
    extra = os.environ.get("HBMX_PROBE_TOOL", "").strip()
    if extra:
        import shlex
        try:
            r = subprocess.run(shlex.split(extra) + [path], capture_output=True, text=True, timeout=120)
            out["probe_tool"] = clip((r.stdout or r.stderr).strip(), 600)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            out["probe_tool"] = f"({extra} failed: {e})"
    return out


# ---------------------------------------------------------------- timestamps
_MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
TS_PATTERNS = {
    "iso8601": re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:[.,](\d{1,9}))?(Z|[+-]\d\d:?\d\d)?"),
    "clf": re.compile(r"\[(\d\d)/([A-Z][a-z]{2})/(\d{4}):(\d\d):(\d\d):(\d\d)(?: ([+-]\d{4}))?\]"),
    "syslog": re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([ \d]\d) (\d\d):(\d\d):(\d\d)\b"),
    "slash": re.compile(r"(\d{4})/(\d\d)/(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?"),
    "epoch_ms": re.compile(r"(?<![\d.])(1[0-9]{12})(?![\d])"),
    "epoch": re.compile(r"(?<![\d.])(1[0-9]{9})(?:\.(\d+))?(?![\d])"),
}
_DAYCACHE: Dict[tuple, int] = {}


def _epoch(y, mo, d, h, mi, s, frac=None, tz=None) -> Optional[float]:
    try:
        key = (y, mo, d)
        base = _DAYCACHE.get(key)
        if base is None:
            if not (1970 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31):
                return None
            base = _DAYCACHE[key] = calendar.timegm((y, mo, d, 0, 0, 0))
        t = base + h * 3600 + mi * 60 + s
        if frac:
            t += int(frac[:6].ljust(6, "0")) / 1e6
        if tz and tz not in ("Z", "z"):
            tz = tz.replace(":", "")
            sign = -1 if tz[0] == "+" else 1
            t += sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
        return float(t)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_ts(kind: str, m: "re.Match", year: int = 1970) -> Optional[float]:
    g = m.groups()
    if kind == "iso8601":
        return _epoch(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5]), g[6], g[7])
    if kind == "clf":
        return _epoch(int(g[2]), _MON.get(g[1], 0), int(g[0]), int(g[3]), int(g[4]), int(g[5]), None, g[6])
    if kind == "syslog":
        return _epoch(year, _MON[g[0]], int(g[1]), int(g[2]), int(g[3]), int(g[4]))
    if kind == "slash":
        return _epoch(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5]), g[6])
    if kind == "epoch_ms":
        return int(g[0]) / 1000.0
    if kind == "epoch":
        return float(g[0] + ("." + g[1] if g[1] else ""))
    return None


def parse_ts_text(s: str, year: int = 1970) -> Optional[float]:
    """A timestamp typed by a user or the LLM: any known pattern, or a bare date / date+hour:minute."""
    s = str(s).strip()
    for kind, rx in TS_PATTERNS.items():
        m = rx.search(s)
        if m:
            return parse_ts(kind, m, year)
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d)(?::(\d\d))?)?", s)
    if m:
        return _epoch(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0), 0)
    return None


def ts_kind_of_value(v) -> Optional[str]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 1e12 <= v < 1e13:
            return "epoch_ms"
        if 1e9 <= v < 1e10:
            return "epoch"
        return None
    if isinstance(v, str):
        for kind in ("iso8601", "clf", "syslog", "slash", "epoch_ms", "epoch"):
            if TS_PATTERNS[kind].search(v[:64]):
                return kind
    return None


def make_ts_fn(spec: Optional[dict], year: int = 1970) -> Optional[Callable[[bytes], Optional[float]]]:
    """bytes line -> epoch seconds (or None), from the timestamp spec saved in the file's structure."""
    if not spec:
        return None
    kind = spec.get("pattern")
    rx = TS_PATTERNS.get(kind)
    if rx is None:
        return None
    via = spec.get("via")
    if via == "regex":
        win = int(spec.get("window") or 100)

        def fn(line: bytes):
            m = rx.search(line[:win].split(b"\n", 1)[0].decode("utf-8", "replace"))
            return parse_ts(kind, m, year) if m else None
        return fn
    if via == "json":
        path = spec.get("key", "").split(".")
        key_rx = re.compile(rb'"' + re.escape(path[-1].encode()) + rb'"\s*:\s*(?:"([^"]*)"|([\d.]+))')

        def fn(line: bytes):
            line = line.split(b"\n", 1)[0]
            try:
                v = json.loads(line)
                for p in path:
                    v = v[p]
            except (KeyError, TypeError, IndexError):
                return None
            except ValueError:  # a truncated head (long line): find the key by pattern
                m = key_rx.search(line)
                if not m:
                    return None
                v = m.group(1).decode("utf-8", "replace") if m.group(1) is not None else float(m.group(2))
            if isinstance(v, (int, float)):
                return v / 1000.0 if kind == "epoch_ms" else float(v)
            m = rx.search(str(v))
            return parse_ts(kind, m, year) if m else None
        return fn
    if via == "csv":
        col, delim = int(spec.get("col", 0)), spec.get("delimiter", ",")

        def fn(line: bytes):
            try:
                row = next(csv.reader([line.split(b"\n", 1)[0].decode("utf-8", "replace").rstrip("\r")], delimiter=delim))
                m = rx.search(row[col])
            except (StopIteration, IndexError, csv.Error):
                return None
            return parse_ts(kind, m, year) if m else None
        return fn
    return None


LEVEL_RX = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRIT|CRITICAL|FATAL|ALERT|EMERG|SEVERE|PANIC)\b")
LEVEL_RX_I = re.compile(r"\b(trace|debug|info|notice|warn|warning|error|err|crit|critical|fatal|alert|emerg|severe|panic)\b", re.I)
LEVEL_NORM = {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "CRIT", "SEVERE": "ERROR", "PANIC": "FATAL", "EMERG": "FATAL",
              "ALERT": "CRIT"}
BAD_LEVELS = ("ERROR", "CRIT", "FATAL", "5xx")


def norm_level(s: str) -> str:
    s = str(s).strip().upper()
    return LEVEL_NORM.get(s, s)


# ---------------------------------------------------------------- the mapping pass
def choose_stride(size: int) -> int:
    want = max(1, size // 4096)
    stride = 1 << max(16, (want - 1).bit_length())
    return min(stride, 8 << 20)


_PRINTABLE = bytes([9, 10, 13] + list(range(32, 127)))


def _cell_bytes_stats(view: bytes) -> Tuple[float, float]:
    """(shannon entropy bits/byte, ascii printable ratio) of a sample."""
    n = len(view)
    if not n:
        return 0.0, 1.0
    printable = n - len(view.translate(None, _PRINTABLE))
    if np is not None:
        c = np.bincount(np.frombuffer(view, dtype=np.uint8), minlength=256)
        p = c[c > 0] / n
        ent = float(-(p * np.log2(p)).sum())
    else:
        ent = 0.0
        for k in Counter(view[:16384]).values():
            p = k / min(n, 16384)
            ent -= p * math.log2(p)
    return round(ent, 2), round(printable / n, 3)


def map_pass(path: str, size: int, stride: int, binary: bool, ctl: Ctl) -> dict:
    """One streaming read: sha256, newline index per cell, keyword densities, entropy, embedded signatures and the
    first bytes of the first full line of every cell (timestamps are parsed from these once the format is known)."""
    h = hashlib.sha256()
    ncell = max(1, -(-size // stride))
    g = {k: [0] * ncell for k in ("nl_before", "nl", "err", "warn", "exc", "nul")}
    g["ent"], g["pr"] = [0.0] * ncell, [1.0] * ncell
    heads: List[Optional[bytes]] = [None] * ncell
    sigs: List[list] = []
    pending: List[int] = []
    total_nl, off, last_byte, tail = 0, 0, b"\n", b""
    t0 = time.time()
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n = len(chunk)
            if pending:
                p = chunk.find(b"\n")
                if p >= 0:
                    if p + 1 < n:
                        for ci in pending:
                            heads[ci] = chunk[p + 1:p + 1 + HEAD]
                    pending = [] if p + 1 < n else pending
            for a in range(0, n, stride):
                ci = (off + a) // stride
                if ci >= ncell:
                    break
                b = min(a + stride, n)
                c = chunk.count(b"\n", a, b)
                g["nl_before"][ci], g["nl"][ci] = total_nl, c
                total_nl += c
                g["err"][ci] = (chunk.count(b"ERROR", a, b) + chunk.count(b"FATAL", a, b) + chunk.count(b"CRIT", a, b)
                                + chunk.count(b'"error"', a, b) + chunk.count(b"=error", a, b))
                g["warn"][ci] = chunk.count(b"WARN", a, b) + chunk.count(b'"warn', a, b) + chunk.count(b"=warn", a, b)
                g["exc"][ci] = chunk.count(b"Exception", a, b) + chunk.count(b"Traceback", a, b)
                g["nul"][ci] = chunk.count(b"\x00", a, b)
                g["ent"][ci], g["pr"][ci] = _cell_bytes_stats(chunk[a:min(b, a + 65536)])
                at_start = (last_byte == b"\n") if a == 0 else (chunk[a - 1:a] == b"\n")
                if at_start:
                    heads[ci] = chunk[a:a + HEAD]
                else:
                    p = chunk.find(b"\n", a)
                    if p >= 0 and p + 1 < n:
                        heads[ci] = chunk[p + 1:p + 1 + HEAD]
                    else:
                        pending.append(ci)
            if binary and len(sigs) < MAX_SIG_HITS:
                hay = tail + chunk
                base = off - len(tail)
                for name, magic in EMBED_SIGS:
                    i = hay.find(magic)
                    while i >= 0 and len(sigs) < MAX_SIG_HITS:
                        if base + i > 0 and (not sigs or sigs[-1] != [name, base + i]):
                            sigs.append([name, base + i])
                        i = hay.find(magic, i + 1)
                tail = chunk[-15:]
            last_byte = chunk[-1:]
            off += n
            rate = off / max(1e-3, time.time() - t0)
            ctl.tick(f"mapping {off * 100 // max(1, size)}% · {human(rate)}/s", off / max(1, size))
    lines = total_nl + (1 if size and last_byte != b"\n" else 0)
    sigs.sort(key=lambda s: s[1])
    return {"sha256": h.hexdigest(), "grid": g, "heads": heads, "lines": lines, "end_nl": last_byte == b"\n",
            "sigs": sigs, "secs": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- sampling
def read_at(path: str, off: int, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(max(0, off))
        return f.read(n)


def sample_lines(path: str, size: int, n_mid: int = 8) -> Tuple[List[bytes], List[bytes], List[bytes]]:
    """(head, middle, tail) lines: the first 256 KB, n_mid slices of 32 KB spread over the file, the last 64 KB.
    Partial lines at slice edges are dropped."""
    head_b = read_at(path, 0, 256 << 10)
    head = head_b.split(b"\n")
    if len(head_b) < size:
        head = head[:-1]
    mid: List[bytes] = []
    if size > 512 << 10:
        for k in range(1, n_mid + 1):
            off = size * k // (n_mid + 1)
            mid.extend(read_at(path, off, 32 << 10).split(b"\n")[1:-1])
    tail: List[bytes] = []
    if size > 256 << 10:
        tail = read_at(path, size - (64 << 10), 64 << 10).split(b"\n")[1:]
        if tail and tail[-1] == b"":
            tail = tail[:-1]
    return head, mid, tail


def is_text(sample: bytes) -> Tuple[bool, str]:
    if not sample:
        return True, "empty"
    if sample[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False, "utf-16 text (BOM): converted views are not supported, treated as binary"
    if b"\x00" in sample:
        return False, f"NUL bytes in the first {human(len(sample))}"
    try:
        sample.decode("utf-8")
        return True, "valid utf-8"
    except UnicodeDecodeError as e:
        if e.start >= len(sample) - 4:
            return True, "valid utf-8"
        printable = (len(sample) - len(sample.translate(None, _PRINTABLE))) / len(sample)
        if printable > 0.95:
            return True, f"mostly ascii ({printable:.0%}), some non-utf-8 bytes"
        return False, f"not utf-8, ascii printable {printable:.0%}"


# ---------------------------------------------------------------- text format detectors
CLF_RX = re.compile(r'^(\S+) (\S+) (\S+) \[([^\]]+)\] "(\S+) ?(\S*) ?([^"]*)" (\d{3}) (\S+)(?: "([^"]*)" "([^"]*)")?')
CLF_FIELDS = ["ip", "ident", "user", "ts", "method", "path", "proto", "status", "size", "referer", "ua"]
SYSLOG_RX = re.compile(r"^(?:<\d+>)?([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (\S+) ([^\[:\s]+)(?:\[(\d+)\])?: ?(.*)$")
LOGFMT_RX = re.compile(r'(?:^|\s)([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S*)')
CODE_EXT = {"py", "js", "ts", "go", "rs", "c", "h", "cpp", "hpp", "java", "kt", "rb", "php", "lua", "sh", "bash", "pl",
            "cs", "swift", "scala", "sql", "vim", "r", "m", "zig"}


def _json_obj(s: str):
    s = s.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def flatten(obj, prefix: str = "", depth: int = 2, out: Optional[dict] = None) -> dict:
    """{"a": {"b": 1}} -> {"a.b": 1}, nested dicts down to `depth` levels; deeper values stay whole."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict) and depth > 1:
                flatten(v, key + ".", depth - 1, out)
            else:
                out[key] = v
    else:
        out[prefix.rstrip(".") or "value"] = obj
    return out


def vtype(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "object"


def guess_type(s: str) -> str:
    s = s.strip()
    if s == "" or s.lower() in ("null", "none", "nan", "na", "n/a", "-"):
        return "empty"
    if re.fullmatch(r"[+-]?\d+", s):
        return "int"
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", s):
        return "float"
    if s.lower() in ("true", "false", "yes", "no"):
        return "bool"
    if ts_kind_of_value(s) in ("iso8601", "clf", "syslog", "slash"):
        return "datetime"
    return "str"


def detect_ts(lines: List[str], window: int = 100) -> Tuple[Optional[str], float]:
    """The timestamp pattern most lines carry near their start, and the share of lines that do."""
    body = [l for l in lines if l.strip() and not l[:1].isspace()]
    if not body:
        return None, 0.0
    best, frac = None, 0.0
    for kind, rx in TS_PATTERNS.items():
        hit = sum(1 for l in body if rx.search(l[:window]))
        f = hit / len(body)
        if kind.startswith("epoch"):
            f *= 0.9  # numbers are weaker evidence than a date
        if f > frac:
            best, frac = kind, f
    return best, frac


def detect_csv(lines: List[str]) -> Optional[dict]:
    rows = [l for l in lines[:600] if l.strip()]
    if len(rows) < 2:
        return None
    best = None
    for d in (",", "\t", ";", "|"):
        if not any(d in r for r in rows[:50]):
            continue
        try:
            counts = [len(r) for r in csv.reader(rows, delimiter=d)]
        except csv.Error:
            continue
        nf, freq = Counter(counts).most_common(1)[0]
        cons = freq / len(counts)
        if nf >= 2 and cons >= 0.9:
            score = cons * min(nf, 20)
            if best is None or score > best["score"]:
                best = {"delimiter": d, "fields": nf, "consistency": round(cons, 3), "score": score}
    return best


def csv_structure(lines: List[str], delim: str) -> dict:
    rows = [r for r in csv.reader([l for l in lines if l.strip()][:2000], delimiter=delim)]
    nf = Counter(len(r) for r in rows).most_common(1)[0][0]
    rows = [r for r in rows if len(r) == nf]
    first, rest = rows[0], rows[1:] or rows
    t_first = [guess_type(c) for c in first]
    t_rest = [Counter(guess_type(r[i]) for r in rest[:500]) for i in range(nf)]
    header = all(t == "str" for t in t_first) and (
        any(tr.most_common(1)[0][0] not in ("str", "empty") for tr in t_rest) or len(set(first)) == nf)
    try:
        header = csv.Sniffer().has_header("\n".join(lines[:50])) or header
    except csv.Error:
        pass
    names = [c.strip() or f"col{i + 1}" for i, c in enumerate(first)] if header else [f"col{i + 1}" for i in range(nf)]
    body = rows[1:] if header else rows
    cols = []
    ts_col = None
    for i, name in enumerate(names):
        vals = [r[i] for r in body[:1000]]
        tc = Counter(guess_type(v) for v in vals)
        nonempty = {k: v for k, v in tc.items() if k != "empty"}
        typ = max(nonempty, key=nonempty.get) if nonempty else "empty"
        if typ == "int" and nonempty.get("float"):
            typ = "float"
        ex = next((v for v in vals if v.strip()), "")
        cols.append({"name": name, "type": typ, "empty": round(tc.get("empty", 0) / max(1, len(vals)), 3),
                     "distinct_in_sample": len(set(vals)), "example": clip(ex, 60)})
        if ts_col is None and typ == "datetime":
            ts_col = i
    out = {"delimiter": delim, "header": header, "columns": cols}
    if ts_col is not None:
        ex = next((r[ts_col] for r in body if r[ts_col].strip()), "")
        out["timestamp"] = {"via": "csv", "col": ts_col, "delimiter": delim, "pattern": ts_kind_of_value(ex),
                            "field": names[ts_col], "example": clip(ex, 40)}
    return out


def jsonl_structure(objs: List[dict]) -> dict:
    keys: Dict[str, Counter] = {}
    examples: Dict[str, str] = {}
    for o in objs:
        for k, v in flatten(o).items():
            keys.setdefault(k, Counter())[vtype(v)] += 1
            if k not in examples and v not in (None, "", [], {}):
                examples[k] = clip(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, 60)
    n = max(1, len(objs))
    rows = sorted(keys.items(), key=lambda kv: -sum(kv[1].values()))
    out = {"keys": [{"key": k, "present": round(sum(c.values()) / n, 3), "types": dict(c.most_common()),
                     "example": examples.get(k, "")} for k, c in rows[:60]]}
    if len(rows) > 60:
        out["keys_more"] = len(rows) - 60
    for name in ("@timestamp", "timestamp", "ts", "time", "datetime", "date", "t", "created_at", "eventTime"):
        if name in keys:
            v = next((flatten(o).get(name) for o in objs if flatten(o).get(name) is not None), None)
            kind = ts_kind_of_value(v)
            if kind:
                out["timestamp"] = {"via": "json", "key": name, "pattern": kind, "field": name, "example": clip(v, 40)}
                break
    for name in ("level", "lvl", "severity", "loglevel", "log.level", "levelname", "priority"):
        if name in keys:
            out["level_key"] = name
            break
    return out


def json_skeleton(path: str, size: int) -> dict:
    """Shape of one big JSON document: parsed fully when small, else the first leaf paths via jq --stream."""
    def shape(v, depth=0):
        if depth >= 3:
            return vtype(v)
        if isinstance(v, dict):
            items = list(v.items())
            s = {k: shape(x, depth + 1) for k, x in items[:30]}
            if len(items) > 30:
                s["…"] = f"+{len(items) - 30} keys"
            return s
        if isinstance(v, list):
            return [f"array[{len(v)}]", shape(v[0], depth + 1)] if v else "array[0]"
        return vtype(v)
    if size <= 64 << 20:
        try:
            with open(path, "rb") as f:
                return {"shape": shape(json.loads(f.read())), "exact": True}
        except ValueError as e:
            return {"error": f"not valid JSON: {e}"}
    if shutil.which("jq"):
        try:
            r = subprocess.run(["jq", "-cn", "--stream", 'limit(20000; inputs | select(length==2) | .[0] | '
                                'map(if type=="number" then "[]" else . end) | join("."))', path],
                               capture_output=True, text=True, timeout=120)
            paths = Counter(r.stdout.split())
            return {"leaf_paths_seen": [{"path": p, "n": n} for p, n in paths.most_common(40)],
                    "exact": False, "note": "first 20,000 leaf values (jq --stream); [] = array index"}
        except (OSError, subprocess.SubprocessError) as e:
            return {"error": f"jq failed: {e}"}
    return {"note": "large JSON document; install jq for a streamed skeleton"}


def detect_text(path: str, size: int, ext: str, head: List[str], mid: List[str], tail: List[str]) -> dict:
    """Identify the format of a text file from samples. Returns identity + structure."""
    lines = [l for l in head + mid + tail]
    body = [l for l in lines if l.strip()]
    ev: List[str] = []
    if not body:
        return {"class": "unstructured", "format": "text", "confidence": 0.5, "evidence": ["no non-empty lines sampled"]}
    first = body[0].lstrip("\ufeff").strip()

    # JSON lines / JSON document
    parsed = [_json_obj(l) for l in body[:300]]
    ok = [p for p in parsed if isinstance(p, dict)]
    frac = len(ok) / len(parsed)
    if frac >= 0.9:
        st = jsonl_structure(ok + [p for p in (_json_obj(l) for l in mid + tail) if isinstance(p, dict)])
        ev.append(f"{frac:.0%} of {len(parsed)} sampled lines are JSON objects")
        return {"class": "structured", "format": "jsonl", "confidence": round(frac, 2), "evidence": ev, "structure": st}
    if first[:1] in "{[" and (ext in ("json", "geojson") or frac < 0.5):
        sk = json_skeleton(path, size)
        if "error" not in sk:
            ev.append("starts with " + first[:1] + ("; parses as one JSON document" if sk.get("exact") else ""))
            return {"class": "structured", "format": "json", "confidence": 0.9, "evidence": ev, "structure": sk}

    # XML / HTML
    if first.startswith("<?xml") or (first.startswith("<") and ext in ("xml", "html", "htm", "svg", "xhtml", "plist", "rss")):
        tags = Counter(re.findall(r"<([A-Za-z_][\w:.\-]*)[\s>/]", "\n".join(lines)))
        root = re.search(r"<([A-Za-z_][\w:.\-]*)[\s>/]", "\n".join(head[:50]).split("?>", 1)[-1])
        record = next((t for t, _ in tags.most_common() if not root or t != root.group(1)), None)
        ev.append("xml prolog" if first.startswith("<?xml") else f"markup, .{ext}")
        return {"class": "structured", "format": "html" if ext.startswith("htm") else "xml", "confidence": 0.9,
                "evidence": ev, "structure": {"root": root.group(1) if root else None, "record_tag_guess": record,
                                              "tags_in_sample": dict(tags.most_common(25))}}

    # Logs (a timestamp near the start of most lines)
    ts_kind, ts_frac = detect_ts(body)
    csvd = detect_csv(head)
    if csvd and (csvd["fields"] >= 3 and csvd["consistency"] >= 0.95 or not (ts_kind and ts_frac >= 0.6)):
        fmt = {"\t": "tsv", ",": "csv", ";": "csv", "|": "psv"}[csvd["delimiter"]]
        st = csv_structure(head + mid + tail, csvd["delimiter"])
        ev.append(f"{csvd['fields']} fields per row on {csvd['consistency']:.0%} of sampled rows "
                  f"(delimiter {csvd['delimiter']!r}), header {'yes' if st['header'] else 'no'}")
        return {"class": "structured", "format": fmt, "confidence": round(csvd["consistency"], 2), "evidence": ev,
                "structure": st}
    if ts_kind and ts_frac >= 0.6:
        st: dict = {"timestamp": {"via": "regex", "pattern": ts_kind, "window": 100}}
        clf = sum(1 for l in body[:400] if CLF_RX.match(l)) / min(len(body), 400)
        sysl = sum(1 for l in body[:400] if SYSLOG_RX.match(l)) / min(len(body), 400)
        lfmt = sum(1 for l in body[:400] if len(LOGFMT_RX.findall(l)) >= 3) / min(len(body), 400)
        levels = Counter(norm_level(m.group(1)) for l in body for m in [LEVEL_RX.search(l[:160])] if m)
        if clf >= 0.6:
            fmt = "log.clf"
            st["fields"] = CLF_FIELDS
            st["levels_from"] = "http status class (2xx..5xx)"
            ev.append(f"{clf:.0%} of lines match the common/combined access-log layout")
        elif sysl >= 0.6:
            fmt = "log.syslog"
            progs = Counter(m.group(3) for l in body for m in [SYSLOG_RX.match(l)] if m)
            st["programs_in_sample"] = dict(progs.most_common(12))
            ev.append(f"{sysl:.0%} of lines match syslog 'Mon dd hh:mm:ss host prog[pid]:'")
        elif lfmt >= 0.6:
            fmt = "log.logfmt"
            keys = Counter(k for l in body[:400] for k, _ in LOGFMT_RX.findall(l))
            st["keys_in_sample"] = dict(keys.most_common(20))
            ev.append(f"{lfmt:.0%} of lines are key=value (logfmt)")
        else:
            fmt = f"log.{ts_kind}"
        ev.append(f"{ts_frac:.0%} of lines carry a {ts_kind} timestamp near the start")
        if levels:
            tot = sum(levels.values())
            st["levels_in_sample"] = {k: round(v / tot, 3) for k, v in levels.most_common()}
        cont = sum(1 for l in lines if l[:1].isspace() and l.strip())
        if cont:
            st["continuation_lines_in_sample"] = cont
            ev.append(f"{cont} indented continuation lines (stack traces / multi-line records)")
        st["example"] = clip(body[0], 200)
        return {"class": "structured", "format": fmt, "confidence": round(min(ts_frac, 0.99), 2), "evidence": ev,
                "structure": st}

    # logfmt without timestamps, yaml, markdown, code, prose
    lfmt = sum(1 for l in body[:400] if len(LOGFMT_RX.findall(l)) >= 3) / min(len(body), 400)
    if lfmt >= 0.7:
        keys = Counter(k for l in body[:400] for k, _ in LOGFMT_RX.findall(l))
        return {"class": "structured", "format": "logfmt", "confidence": round(lfmt, 2),
                "evidence": [f"{lfmt:.0%} of lines are key=value"], "structure": {"keys_in_sample": dict(keys.most_common(20))}}
    if ext in ("yml", "yaml") or (sum(1 for l in body[:300] if re.match(r"^\s*(- )?[\w.\-\"']+:(\s|$)", l)) / min(len(body), 300) > 0.7):
        return {"class": "semi-structured", "format": "yaml", "confidence": 0.8, "evidence": ["key: value lines"],
                "structure": {"top_keys_in_sample": [l.split(":")[0] for l in body[:400] if re.match(r"^[\w.\-]+:", l)][:30]}}
    if ext in ("md", "markdown", "rst") or sum(1 for l in body if re.match(r"^#{1,6} ", l)) >= 3:
        return {"class": "semi-structured", "format": "markdown", "confidence": 0.8,
                "evidence": [f".{ext}" if ext else "markdown headings"], "structure": {"toc": "headings (see regions)"}}
    if ext in CODE_EXT or first.startswith("#!"):
        return {"class": "semi-structured", "format": f"code.{ext or 'script'}", "confidence": 0.8,
                "evidence": [f".{ext}" if ext else "shebang"], "structure": {"toc": "definitions (see regions)"}}
    lens = [len(l) for l in body]
    return {"class": "unstructured", "format": "text", "confidence": 0.6,
            "evidence": ["no record layout, timestamps or markup found in the samples"],
            "structure": {"line_length_in_sample": {"avg": round(sum(lens) / len(lens)), "max": max(lens)},
                          "blank_line_ratio": round(1 - len(body) / max(1, len(lines)), 3)}}


# ---------------------------------------------------------------- binary formats
def gguf_structure(path: str) -> dict:
    TYPES = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    GT = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K",
          12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
          20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16", 26: "I32", 27: "I64", 28: "F64",
          29: "IQ1_M", 30: "BF16"}
    with open(path, "rb") as f:
        def rd(fmt):
            n = struct.calcsize("<" + fmt)
            return struct.unpack("<" + fmt, f.read(n))[0]

        def rstr():
            n = rd("Q")
            if n > 1 << 20:
                raise ValueError("string too long")
            return f.read(n).decode("utf-8", "replace")

        def rval(t):
            if t == 8:
                return rstr()
            if t == 9:
                at, n = rd("I"), rd("Q")
                if at in TYPES:
                    f.seek(n * struct.calcsize("<" + TYPES[at]), 1)
                else:
                    for _ in range(n):
                        rval(at)
                return f"[array of {n} {('string' if at == 8 else TYPES.get(at, at))}]"
            return rd(TYPES[t])

        f.read(4)
        version, n_tensors, n_kv = rd("I"), rd("Q"), rd("Q")
        kv = {}
        for _ in range(min(n_kv, 10000)):
            k = rstr()
            kv[k] = rval(rd("I"))
        kv_end = f.tell()
        tensors, types = [], Counter()
        for _ in range(min(n_tensors, 100000)):
            name = rstr()
            dims = [rd("Q") for _ in range(rd("I"))]
            typ, off = rd("I"), rd("Q")
            types[GT.get(typ, str(typ))] += 1
            if len(tensors) < 12:
                tensors.append({"name": name, "dims": dims, "type": GT.get(typ, str(typ)), "offset": off})
        info_end = f.tell()
    align = int(kv.get("general.alignment", 32) or 32)
    data_start = -(-info_end // align) * align
    small = {k: (clip(v, 80) if isinstance(v, str) else v) for k, v in kv.items()}
    return {"version": version, "tensors": n_tensors, "kv_count": n_kv, "metadata": small,
            "tensor_types": dict(types.most_common()), "first_tensors": tensors,
            "layout": {"header_kv": [0, kv_end], "tensor_info": [kv_end, info_end], "tensor_data": [data_start, None]}}


def safetensors_structure(path: str) -> dict:
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n))
    meta = hdr.pop("__metadata__", {})
    dt = Counter(v.get("dtype") for v in hdr.values())
    tensors = sorted(hdr.items(), key=lambda kv: -(kv[1]["data_offsets"][1] - kv[1]["data_offsets"][0]))
    return {"tensors": len(hdr), "dtypes": dict(dt), "metadata": meta, "header_bytes": n,
            "largest_tensors": [{"name": k, "shape": v.get("shape"), "dtype": v.get("dtype"),
                                 "bytes": [8 + n + v["data_offsets"][0], 8 + n + v["data_offsets"][1]]}
                                for k, v in tensors[:20]]}


def sqlite_structure(path: str) -> dict:
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        out = []
        for name, typ, sql in con.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') "
                                          "ORDER BY name").fetchall()[:200]:
            cols = [f"{r[1]} {r[2]}".strip() for r in con.execute(f'PRAGMA table_info("{name}")').fetchall()]
            rows = None
            if typ == "table":
                try:
                    rows = con.execute(f'SELECT max(rowid) FROM "{name}"').fetchone()[0]
                except Exception:
                    rows = None
            out.append({"name": name, "type": typ, "columns": cols[:40], "rows_approx": rows})
        idx = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
        return {"tables": out, "indexes": len(idx), "page_size": con.execute("PRAGMA page_size").fetchone()[0]}
    finally:
        con.close()


def zip_structure(path: str) -> dict:
    import zipfile
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    ext = Counter(os.path.splitext(i.filename)[1].lower() or "(none)" for i in infos if not i.is_dir())
    big = sorted(infos, key=lambda i: -i.file_size)[:30]
    return {"members": len(infos), "uncompressed": sum(i.file_size for i in infos), "extensions": dict(ext.most_common(15)),
            "largest": [{"name": i.filename, "size": i.file_size, "compressed": i.compress_size,
                         "bytes": [i.header_offset, i.header_offset + 30 + len(i.filename) + i.compress_size]} for i in big]}


def tar_structure(path: str, limit: int = 20000) -> dict:
    import tarfile
    members, total, ext = [], 0, Counter()
    with tarfile.open(path, "r:") as t:
        for i, m in enumerate(t):
            if i >= limit:
                break
            total += m.size
            if m.isfile():
                ext[os.path.splitext(m.name)[1].lower() or "(none)"] += 1
            members.append(m)
    big = sorted(members, key=lambda m: -m.size)[:30]
    return {"members_listed": len(members), "complete": len(members) < limit, "content_bytes": total,
            "extensions": dict(ext.most_common(15)),
            "largest": [{"name": m.name, "size": m.size, "bytes": [m.offset_data, m.offset_data + m.size]} for m in big]}


DECOMP = {"gzip": ["gzip", "-dc"], "bzip2": ["bzip2", "-dc"], "xz": ["xz", "-dc"], "zstd": ["zstd", "-dcq"],
          "lz4": ["lz4", "-dcq"]}


def decompress_head(path: str, sig: str, n: int = 4 << 20) -> bytes:
    if sig == "gzip":
        with gzip.open(path, "rb") as f:
            return f.read(n)
    if sig == "bzip2":
        import bz2
        with bz2.open(path, "rb") as f:
            return f.read(n)
    if sig == "xz":
        import lzma
        with lzma.open(path, "rb") as f:
            return f.read(n)
    cmd = DECOMP.get(sig)
    if not cmd or not shutil.which(cmd[0]):
        raise HbmxError(f"no decompressor for {sig} ({cmd[0] if cmd else '?'} not found)")
    p = subprocess.Popen(cmd + [path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = p.stdout.read(n)
    p.kill()
    p.wait()
    return data


def compressed_structure(path: str, sig: str, size: int) -> dict:
    out: dict = {"codec": sig}
    try:
        head = decompress_head(path, sig)
    except Exception as e:
        return {"codec": sig, "error": str(e)}
    out["decompressed_sample"] = len(head)
    if sig == "gzip":
        with open(path, "rb") as f:
            hdr = f.read(512)
            f.seek(-4, 2)
            isize = struct.unpack("<I", f.read(4))[0]
        out["uncompressed_mod_4GB"] = isize
        if hdr[3] & 0x08:
            name = hdr[10:hdr.find(b"\x00", 10)]
            out["original_name"] = name.decode("latin-1")
    elif sig == "xz" and shutil.which("xz"):
        try:
            r = subprocess.run(["xz", "--robot", "--list", path], capture_output=True, text=True, timeout=60)
            tot = [l for l in r.stdout.splitlines() if l.startswith("totals")]
            if tot:
                out["uncompressed"] = int(tot[0].split("\t")[4])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
    inner = match_sig(head[:1024])
    if inner:
        out["inner"] = {"format": inner[0], "family": inner[1]}
        return out
    txt, why = is_text(head[:1 << 20])
    if txt:
        lines = head.decode("utf-8", "replace").split("\n")[:-1]
        d = detect_text(path, len(head), "", lines, [], [])
        out["inner"] = {"format": d["format"], "class": d["class"], "confidence": d["confidence"],
                        "evidence": d.get("evidence"), "structure": d.get("structure")}
    else:
        out["inner"] = {"format": "binary", "why": why}
    return out


def terrain_class(ent: float, pr: float, nul_ratio: float) -> str:
    if nul_ratio > 0.9:
        return "zero-fill"
    if pr > 0.9:
        return "text-like"
    if ent > 7.5:
        return "compressed/encrypted"
    if ent < 2.0:
        return "sparse/padding"
    return "binary-structured"


# ---------------------------------------------------------------- the entry (one file in memory)
class Entry:
    def __init__(self, sha: str, meta: dict, path: Optional[str]):
        self.sha, self.meta, self.path = sha, meta, path
        self._grid: Optional[dict] = None

    # ----- paths
    @property
    def meta_file(self):
        return meta_path(self.sha)

    @property
    def edir(self):
        return entry_dir(self.sha)

    @property
    def fdir(self):
        return func_dir(self.sha)

    @property
    def ddir(self):
        return data_dir(self.sha)

    @property
    def name(self):
        return os.path.basename(self.path or "") or self.sha[:12]

    @property
    def fmt(self) -> str:
        return self.meta["identity"]["format"]

    @property
    def size(self) -> int:
        return int(self.meta.get("size") or 0)

    @property
    def lines(self) -> int:
        return int(self.meta.get("lines") or 0)

    @property
    def text(self) -> bool:
        return bool(self.meta["identity"].get("text"))

    @property
    def codec(self) -> Optional[str]:
        return (self.meta.get("structure") or {}).get("codec") if self.meta["identity"].get("family") == "compressed" else None

    @property
    def line_format(self) -> str:
        """The format of the line stream: the inner format for a compressed text file."""
        if self.codec:
            return ((self.meta.get("structure") or {}).get("inner") or {}).get("format", "binary")
        return self.fmt

    @property
    def line_structure(self) -> dict:
        st = self.meta.get("structure") or {}
        if self.codec:
            return (st.get("inner") or {}).get("structure") or {}
        return st

    @property
    def random_access(self) -> bool:
        return self.text and not self.codec

    @property
    def grid(self) -> dict:
        if self._grid is None:
            try:
                with open(os.path.join(self.ddir, "grid.json"), encoding="utf-8") as f:
                    self._grid = json.load(f)
            except (OSError, ValueError):
                self._grid = {}
        return self._grid

    def year(self) -> int:
        try:
            return time.gmtime(os.stat(self.path).st_mtime).tm_year if self.path else 1970
        except OSError:
            return 1970

    def ts_fn(self):
        return make_ts_fn(self.line_structure.get("timestamp"), self.year())

    def save(self):
        self.meta["updated"] = now_iso()
        write_atomic(self.meta_file, ydump(self.meta))

    def require_file(self):
        if not self.path or not os.path.isfile(self.path):
            raise HbmxError(f"the file is not on disk any more (memory only): {self.path}")

    def require_lines(self):
        self.require_file()
        if not self.text:
            raise HbmxError(f"{self.fmt} is binary: use view byte=…, map, functions (no line view)")

    # ----- line index
    def line_offset(self, L: int) -> Optional[int]:
        """Byte offset where line L (1-based) starts, from the cell index; reads at most one cell."""
        self.require_lines()
        if L <= 1:
            return 0
        if L > self.lines:
            return None
        if self.codec:
            raise HbmxError("compressed: no random access by line (search/stats stream it; 'inflate' for views)")
        g = self.grid
        nlb, stride = g["nl_before"], g["stride"]
        target = L - 1  # position right after the target-th newline
        i = max(0, bisect.bisect_right(nlb, target - 1) - 1)
        pos, need = i * stride, target - nlb[i]
        with open(self.path, "rb") as f:
            f.seek(pos)
            while need > 0:
                buf = f.read(1 << 20)
                if not buf:
                    return None
                c = buf.count(b"\n")
                if c < need:
                    need -= c
                    pos += len(buf)
                    continue
                idx = -1
                for _ in range(need):
                    idx = buf.find(b"\n", idx + 1)
                return pos + idx + 1
        return pos

    def byte_line(self, B: int) -> int:
        """Line number containing byte B."""
        self.require_lines()
        if self.codec:
            raise HbmxError("compressed: no random access")
        g = self.grid
        stride = g["stride"]
        i = min(max(0, B // stride), len(g["nl_before"]) - 1)
        start = i * stride
        with open(self.path, "rb") as f:
            f.seek(start)
            c = f.read(max(0, B - start)).count(b"\n")
        return g["nl_before"][i] + c + 1

    def cell_of_line(self, L: int) -> int:
        nlb = self.grid["nl_before"]
        return max(0, bisect.bisect_right(nlb, L - 1) - 1)

    def line_range(self, spec) -> Tuple[int, int]:
        """'A:B', 'A:', ':B', [A,B], a region id (r1.2) or zone id (z3) -> (first, last) lines, clamped."""
        if spec in (None, "", "all"):
            return 1, self.lines
        if isinstance(spec, (list, tuple)) and len(spec) == 2:
            a, b = spec
        else:
            s = str(spec).strip()
            span = re.fullmatch(r"([rz][\d.]+)\s*(?::|-|\.\.)\s*([rz][\d.]+)", s)
            if span:  # z30:z40 — from the first line of one zone/region to the last line of another
                n1, n2 = self.find_node(span.group(1)), self.find_node(span.group(2))
                if not (n1 and n2 and n1.get("lines") and n2.get("lines")):
                    raise HbmxError(f"unknown region/zone in {s}")
                return max(1, n1["lines"][0]), min(self.lines, n2["lines"][1])
            if re.fullmatch(r"[rz][\d.]+", s):
                node = self.find_node(s)
                if not node or not node.get("lines"):
                    raise HbmxError(f"unknown region/zone {s} (or it has no line range)")
                a, b = node["lines"]
            else:
                m = re.fullmatch(r"(\d*)\s*(?::|-|\.\.)\s*(\d*)", s.replace(",", ""))
                if not m:
                    raise HbmxError(f"bad line range {s!r} (use A:B, A:, :B, r1.2 or z3)")
                a, b = m.group(1), m.group(2)
        a = int(a) if a not in (None, "") else 1
        b = int(b) if b not in (None, "") else self.lines
        a, b = max(1, a), min(self.lines, b)
        if a > b:
            raise HbmxError(f"empty line range {a}:{b} (file has {num(self.lines)} lines)")
        return a, b

    def find_node(self, nid: str) -> Optional[dict]:
        m = self.meta.get("map") or {}
        if nid.startswith("z"):
            for z in m.get("zones") or []:
                if z["id"] == nid:
                    return z
            sub = re.fullmatch(r"(z\d+)\.(\d+)", nid)
            if sub:
                for z in zoom_zones(self, sub.group(1)):
                    if z["id"] == nid:
                        return z
            return None

        def walk(nodes):
            for n in nodes or []:
                if n.get("id") == nid:
                    return n
                r = walk(n.get("children"))
                if r:
                    return r
            return None
        return walk(m.get("regions"))

    def landmark(self, lid: str) -> Optional[dict]:
        for l in self.meta.get("landmarks") or []:
            if l.get("id") == lid:
                return l
        return None


# ---------------------------------------------------------------- zones (the uniform grid, zoomable)
def _zones(e: Entry, c0: int, c1: int, prefix: str, nmax: int = MAX_ZONES) -> List[dict]:
    g = e.grid
    if not g:
        return []
    stride, ncell = g["stride"], len(g["nl"])
    c1 = min(c1, ncell)
    span = max(1, c1 - c0)
    k = min(nmax, span)
    out = []
    ts = g.get("ts") or []
    for z in range(k):
        a, b = c0 + span * z // k, c0 + span * (z + 1) // k
        if a >= b:
            continue
        lo, hi = a * stride, min(b * stride, e.size)
        zone = {"id": f"{prefix}{z + 1}", "bytes": [lo, hi]}
        if e.text and not e.codec:
            l0 = g["nl_before"][a] + 1
            l1 = g["nl_before"][b] if b < ncell else e.lines
            zone["lines"] = [l0, max(l0, l1)]
            zone["err"] = sum(g["err"][a:b])
            zone["warn"] = sum(g["warn"][a:b])
            zone["exc"] = sum(g["exc"][a:b])
            t = next((ts[i] for i in range(a, b) if i < len(ts) and ts[i] is not None), None)
            if t is not None:
                zone["ts"] = iso(t)
        else:
            n = b - a
            ent = sum(g["ent"][a:b]) / n
            pr = sum(g["pr"][a:b]) / n
            nul = sum(g["nul"][a:b]) / max(1, hi - lo)
            zone.update({"entropy": round(ent, 2), "printable": round(pr, 2), "class": terrain_class(ent, pr, nul)})
        out.append(zone)
    return out


def zoom_zones(e: Entry, zid: str) -> List[dict]:
    z = next((z for z in (e.meta.get("map") or {}).get("zones") or [] if z["id"] == zid), None)
    if not z:
        return []
    stride = e.grid["stride"]
    return _zones(e, z["bytes"][0] // stride, -(-z["bytes"][1] // stride), f"{zid}.", 32)


# ---------------------------------------------------------------- building the map
def _scan_first_last(e: Entry, cells: List[int], rx: "re.Pattern", last: bool) -> Optional[Tuple[int, int, str]]:
    """First (or last) line matching rx inside one cell, as (line, byte, text)."""
    g = e.grid
    stride = g["stride"]
    for ci in cells:
        start, end = ci * stride, min((ci + 1) * stride, e.size)
        with open(e.path, "rb") as f:
            f.seek(start)
            buf = f.read(end - start + (64 << 10))
        lo = 0
        if start > 0:
            p = buf.find(b"\n")
            lo = p + 1 if p >= 0 else len(buf)
        hits = []
        pos = lo
        line_no = g["nl_before"][ci] + 1 + (1 if start > 0 and lo > 0 else 0)
        for raw in buf[lo:].split(b"\n"):
            if pos > end - start:
                break
            if rx.search(raw.decode("utf-8", "replace")):
                hits.append((line_no, start + pos, show_line(raw, 160)))
                if not last:
                    break
            pos += len(raw) + 1
            line_no += 1
        if hits:
            return hits[-1] if last else hits[0]
    return None


def _time_children(e: Entry, lo_line: int, hi_line: int) -> List[dict]:
    """Children of the record region by calendar unit (hour/day/month), boundaries exact to the line."""
    g = e.grid
    ts = g.get("ts") or []
    pts = [(i, t) for i, t in enumerate(ts) if t is not None]
    if len(pts) < 2:
        return []
    t0, t1 = pts[0][1], pts[-1][1]
    span = t1 - t0
    if span <= 0:
        return []
    unit, fmt = (3600, "%Y-%m-%d %H:00") if span <= 2 * 86400 else (86400, "%Y-%m-%d") if span <= 62 * 86400 else (None, "%Y-%m")
    key = (lambda t: int(t // unit)) if unit else (lambda t: time.strftime("%Y-%m", time.gmtime(t)))
    bounds = []  # (cell index where a new period starts, label)
    prev = None
    for i, t in pts:
        k = key(t)
        if k != prev:
            bounds.append((i, time.strftime(fmt, time.gmtime(t)), k))
            prev = k
    if len(bounds) > 120 or len(bounds) < 2:
        return []
    fn = e.ts_fn()
    out = []
    starts = []
    for n, (ci, label, k) in enumerate(bounds):
        if n == 0:
            starts.append((lo_line, label))
            continue
        # the period starts somewhere in cell ci-1 or at its first line: scan the previous cell for the exact line
        line = _first_line_where(e, max(0, ci - 1), ci, lambda t, k=k: t is not None and key(t) >= k, fn)
        starts.append((line or g["nl_before"][ci] + 1, label))
    for n, (line, label) in enumerate(starts):
        end = (starts[n + 1][0] - 1) if n + 1 < len(starts) else hi_line
        if end < line:
            continue
        out.append({"id": None, "name": label, "lines": [line, end]})
    return out


def _first_line_where(e: Entry, c0: int, c1: int, pred, fn) -> Optional[int]:
    g = e.grid
    stride = g["stride"]
    start, end = c0 * stride, min((c1 + 1) * stride, e.size)
    with open(e.path, "rb") as f:
        f.seek(start)
        buf = f.read(end - start)
    lo = 0
    if start > 0:
        p = buf.find(b"\n")
        lo = p + 1 if p >= 0 else len(buf)
    line_no = g["nl_before"][c0] + 1 + (1 if start > 0 and lo > 0 else 0)
    for raw in buf[lo:].split(b"\n"):
        if pred(fn(raw)):
            return line_no
        line_no += 1
    return None


def _find_jump(e: Entry, c0: int, c1: int, thr: float, fn) -> Optional[Tuple[int, float, float]]:
    """The exact line where time jumps forward by >= thr seconds, between the first lines of cells c0 and c1."""
    g = e.grid
    stride = g["stride"]
    start = c0 * stride
    end = min(c1 * stride + (1 << 20), e.size)
    with open(e.path, "rb") as f:
        f.seek(start)
        buf = f.read(end - start)
    lo = 0
    if start > 0:
        p = buf.find(b"\n")
        lo = p + 1 if p >= 0 else len(buf)
    line_no = g["nl_before"][c0] + 1 + (1 if start > 0 and lo > 0 else 0)
    prev = None
    for raw in buf[lo:].split(b"\n"):
        t = fn(raw)
        if t is not None:
            if prev is not None and t - prev >= thr:
                return line_no, prev, t
            prev = t
        line_no += 1
    return None


TOC_RX = {
    "markdown": r"^#{1,3} \S",
    "code": r"^(?:\s{0,4})(?:def |class |func |fn |function |sub |local function |public |private |impl |struct |interface |type \w+ (?:struct|interface))",
    "text": r"^(?:(?:CHAPTER|Chapter|SECTION|Section|PART|Part|ARTICLE|Article) [\dIVXLC]+\b.{0,60}|\d+(?:\.\d+){0,2}\.? [A-Z][^.]{2,60}|[A-Z][A-Z0-9 ,'&-]{4,60})$",
}


def toc(e: Entry, limit: int = 400) -> List[dict]:
    fam = "code" if e.fmt.startswith("code.") else e.fmt if e.fmt in TOC_RX else None
    if not fam or not shutil.which("rg"):
        return []
    try:
        r = subprocess.run(["rg", "-n", "-b", "--no-heading", "-m", str(limit), "--max-columns", "120", "-e", TOC_RX[fam],
                            e.path], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for l in r.stdout.splitlines():
        parts = l.split(":", 2)
        if len(parts) == 3 and parts[0].isdigit():
            out.append({"line": int(parts[0]), "byte": int(parts[1]), "title": clip(parts[2], 100)})
    return out


def build_map(e: Entry, sigs: List[list], ctl: Ctl):
    """Regions (semantic index tree), zones (uniform grid), landmarks and howto, from structure + grid."""
    g, st, meta = e.grid, e.meta.get("structure") or {}, e.meta
    regions: List[dict] = []
    landmarks: List[dict] = []
    zones = _zones(e, 0, len(g.get("nl") or [1]), "z")
    lf = e.line_format
    if e.text and not e.codec and e.lines:
        first_rec = 1
        if lf in ("csv", "tsv", "psv") and st.get("header"):
            cols = [c["name"] for c in st.get("columns", [])]
            regions.append({"id": "r1", "name": "header", "lines": [1, 1], "what": f"{len(cols)} columns: " + clip(", ".join(cols), 160)})
            first_rec = 2
        rec = {"id": f"r{len(regions) + 1}", "name": "records" if e.meta["identity"]["class"] == "structured" else "body",
               "lines": [first_rec, e.lines]}
        rec["what"] = f"{num(e.lines - first_rec + 1)} {'records' if rec['name'] == 'records' else 'lines'}"
        ctl.tick("regions", 0.0, True)
        kids = []
        if st.get("timestamp") and g.get("ts_sorted"):
            kids = _time_children(e, first_rec, e.lines)
        elif lf in ("markdown", "text") or lf.startswith("code."):
            heads = toc(e)
            if heads:
                save_json(e, "toc.json", heads)
                top = [h for h in heads if not lf == "markdown" or h["title"].startswith("# ")] or heads
                if len(top) > 40:
                    top = [top[i * len(top) // 40] for i in range(40)]
                for n, h in enumerate(top):
                    end = (top[n + 1]["line"] - 1) if n + 1 < len(top) else e.lines
                    kids.append({"id": None, "name": h["title"], "lines": [h["line"], max(h["line"], end)]})
                rec["what"] += f" · {len(heads)} headings/definitions (toc.json)"
        for n, k in enumerate(kids):
            k["id"] = f"{rec['id']}.{n + 1}"
        if kids:
            rec["children"] = kids
        regions.append(rec)
        # landmarks from the grid: first/last error, hotspots, time gaps
        errs = g.get("err") or []
        hot = [i for i, v in enumerate(errs) if v]
        lvl = re.compile(r"\b(ERROR|FATAL|CRIT|CRITICAL)\b|\"(?:level|severity)\"\s*:\s*\"(?:error|fatal|critical)\"|level=(?:error|fatal)", re.I)
        if hot:
            first = _scan_first_last(e, hot[:6], lvl, last=False)
            last = _scan_first_last(e, list(reversed(hot))[:6], lvl, last=True)
            for lab, hit in (("first error", first), ("last error", last)):
                if hit:
                    landmarks.append({"label": lab, "line": hit[0], "byte": hit[1], "text": hit[2], "by": "hbmx"})
            zerr = sorted(z["err"] for z in zones)
            med = zerr[len(zerr) // 2] if zerr else 0
            for z in zones:
                if z["err"] >= 10 and z["err"] >= 5 * max(1, med):
                    landmarks.append({"label": f"error hotspot {z['id']} ({num(z['err'])} error keywords)",
                                      "line": z["lines"][0], "zone": z["id"], "by": "hbmx"})
        ts = g.get("ts") or []
        pts = [(i, t) for i, t in enumerate(ts) if t is not None]
        if len(pts) > 8 and g.get("ts_sorted"):
            deltas = sorted(max(0.0, pts[k + 1][1] - pts[k][1]) for k in range(len(pts) - 1))
            med = deltas[len(deltas) // 2]
            fn = e.ts_fn()
            for k in range(len(pts) - 1):
                d = pts[k + 1][1] - pts[k][1]
                if d > max(600, 20 * med) and len(landmarks) < 30:
                    jump = _find_jump(e, pts[k][0], pts[k + 1][0], max(600, 10 * med), fn)
                    if jump:
                        line, t_a, t_b = jump
                        landmarks.append({"label": f"time gap {dur(t_b - t_a)}: {iso(t_a)} → {iso(t_b)}",
                                          "line": line, "by": "hbmx"})
        tail_line = e.lines
        landmarks.insert(0, {"label": "first line", "line": 1, "byte": 0, "by": "hbmx"})
        landmarks.append({"label": "last line", "line": tail_line, "by": "hbmx"})
    else:
        fam = e.meta["identity"].get("family")
        if e.fmt == "gguf" and "layout" in st:
            lay = st["layout"]
            regions += [{"id": "r1", "name": "header + metadata kv", "bytes": lay["header_kv"], "what": f"{st['kv_count']} keys"},
                        {"id": "r2", "name": "tensor infos", "bytes": lay["tensor_info"], "what": f"{num(st['tensors'])} tensors"},
                        {"id": "r3", "name": "tensor data", "bytes": [lay["tensor_data"][0], e.size],
                         "what": ", ".join(f"{k}×{v}" for k, v in list(st.get("tensor_types", {}).items())[:6])}]
        elif e.fmt == "safetensors":
            regions += [{"id": "r1", "name": "json header", "bytes": [8, 8 + st["header_bytes"]], "what": f"{st['tensors']} tensors"},
                        {"id": "r2", "name": "tensor data", "bytes": [8 + st["header_bytes"], e.size],
                         "children": [{"id": f"r2.{n + 1}", "name": t["name"], "bytes": t["bytes"],
                                       "what": f"{t['dtype']} {t['shape']}"} for n, t in enumerate(st.get("largest_tensors", [])[:20])]}]
        elif e.fmt in ("zip", "tar"):
            regions += [{"id": f"r{n + 1}", "name": m["name"], "bytes": m["bytes"], "what": human(m["size"])}
                        for n, m in enumerate(st.get("largest", [])[:30])]
        elif e.fmt == "sqlite":
            regions += [{"id": f"r{n + 1}", "name": t["name"], "what": f"{t['type']}, ≈{num(t['rows_approx'])} rows, "
                         + clip(", ".join(t["columns"]), 120)} for n, t in enumerate(st.get("tables", [])[:40])]
        if fam in (None, "unknown") or not regions:
            # terrain: merge consecutive cells of the same class (the map of an unknown land)
            stride, n = g["stride"], len(g["ent"])
            cur = None
            for i in range(n):
                lo, hi = i * stride, min((i + 1) * stride, e.size)
                cls = terrain_class(g["ent"][i], g["pr"][i], g["nul"][i] / max(1, hi - lo))
                if cur and cur["class"] == cls:
                    cur["bytes"][1] = hi
                    cur["_e"].append(g["ent"][i])
                else:
                    cur = {"class": cls, "bytes": [lo, hi], "_e": [g["ent"][i]]}
                    regions.append(cur)
            if len(regions) > 60:  # too fragmented: keep the uniform zones as the map
                regions = []
            for n_, r in enumerate(regions):
                ents = r.pop("_e", [])
                cls = r.pop("class", "")
                r.update({"id": f"r{n_ + 1}", "name": cls, "what": f"{human(r['bytes'][1] - r['bytes'][0])}, entropy "
                          f"{sum(ents) / max(1, len(ents)):.2f}"})
        for name, off in sigs[:40]:
            landmarks.append({"label": f"embedded {name} signature", "byte": off, "by": "hbmx"})
    for n, l in enumerate(landmarks):
        l["id"] = f"L{n + 1}"
    meta["map"] = {"zones": zones, "regions": regions}
    meta["landmarks"] = landmarks
    meta["howto"] = howto(e)


def howto(e: Entry) -> List[str]:
    lf, st = e.line_format, e.line_structure
    h = []
    if e.codec:
        h.append(f"{e.codec}-compressed {lf}: search/stats stream through the decompressor (rg -z); 'inflate' gives a "
                 "random-access copy for line views and the full map")
    if e.random_access:
        h.append("views: view line=N count=M (line index, instant at any depth); map zone=zN zooms; search/stats for counts")
    if st.get("timestamp") and e.codec:
        h.append(f"time: {st['timestamp'].get('pattern')} timestamps; seek by time needs the inflated copy")
    elif st.get("timestamp"):
        sorted_ = (e.grid or {}).get("ts_sorted")
        h.append(f"time: {st['timestamp'].get('pattern')} timestamps" + (" in order → seek time=… and the 'between' "
                 "function jump by time" if sorted_ else " NOT in order → use search/filter on time, not seek"))
    if lf.startswith("log"):
        h.append("logs: stats kind=levels|timeline|templates (pattern discovery) |security; zones show error density")
    if lf in ("csv", "tsv", "psv"):
        h.append("table: stats kind=columns, filter where=[\"col>=x\"], record line=N for one parsed row")
    if lf == "jsonl":
        h.append("json lines: stats kind=keys, filter where=[\"a.b=x\"], record line=N; jq function for jq expressions")
    if lf == "json":
        h.append("one JSON document: no record lines; use the jq function (streams with --stream for big files)")
    if e.fmt == "sqlite":
        h.append("sqlite: run the 'sql' function with a read-only query (LIMIT is added)")
    if e.fmt in ("zip", "tar"):
        h.append("archive: 'members' function lists entries; extract a member with the 'member' function")
    if not e.text and not e.codec and e.meta["identity"].get("family") == "unknown":
        h.append("binary: view byte=N length=256 (hex); 'strings' function for text inside a byte range; regions are "
                 "the terrain (entropy classes), landmarks the embedded signatures")
    return h


def save_json(e: Entry, name: str, obj):
    os.makedirs(e.ddir, exist_ok=True)
    write_atomic(os.path.join(e.ddir, name), json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------- template functions (written at probe time)
def _fn_script(e: Entry, name: str, what: str, usage: str, body: str) -> str:
    return (f"#!/usr/bin/env bash\n"
            f"# hbmx function «{name}» for {e.fmt} file {e.sha[:12]} ({e.name})\n"
            f"# what : {what}\n# usage: {usage}\n# by   : hbmx (template)\n"
            f"HBMX_PYTHON=\"${{HBMX_PYTHON:-{PYTHON}}}\"\nHBMX_CORE=\"${{HBMX_CORE:-{CORE}}}\"\n"
            f"HBMX_FILE=\"${{HBMX_FILE:-$(\"$HBMX_PYTHON\" \"$HBMX_CORE\" locate {e.sha})}}\"\n"
            f"[ -f \"$HBMX_FILE\" ] || {{ echo \"hbmx: file {e.sha[:12]} not found on disk\" >&2; exit 2; }}\n"
            f"{body}\n")


def template_functions(e: Entry) -> List[dict]:
    q = f'"$HBMX_PYTHON" "$HBMX_CORE"'
    lf = e.line_format
    T = []

    def add(name, what, usage, body):
        T.append({"name": name, "description": what, "usage": usage, "body": body})

    if e.random_access:
        add("lines", "numbered lines START..START+COUNT-1 (line index, clipped)", "lines <start> [count]",
            f'exec {q} view "$HBMX_FILE" --line "${{1:?start line}}" --count "${{2:-40}}"')
        add("grep", "matching lines with line/byte numbers (ripgrep, bounded)", "grep <regex> [max]",
            f'exec {q} search "$HBMX_FILE" "${{1:?regex}}" --max "${{2:-30}}"')
    if e.codec and lf != "binary":
        add("zcat", "first COUNT decompressed lines", "zcat [count]",
            f'{" ".join(DECOMP.get(e.codec, ["cat"]))} "$HBMX_FILE" 2>/dev/null | head -n "${{1:-40}}" | cut -c1-300')
        add("zgrep", "matching decompressed lines (rg -z), bounded", "zgrep <regex> [max]",
            f'exec rg -z -n --max-columns 300 --max-columns-preview -m "${{2:-30}}" -e "${{1:?regex}}" "$HBMX_FILE"')
    add("bytes", "hex dump of a byte range", "bytes <offset> [length]",
        f'exec xxd -s "${{1:?offset}}" -l "${{2:-256}}" "$HBMX_FILE"')
    if (e.line_structure.get("timestamp") and e.random_access and (e.grid or {}).get("ts_sorted")):
        add("between", "lines between two times (seek by time with the index), first MAX shown + the line range",
            "between <from> <to> [max]",
            f'exec {q} view "$HBMX_FILE" --time "${{1:?from}}..${{2:?to}}" --count "${{3:-40}}"')
    if lf.startswith("log"):
        for k, w in (("levels", "count of lines per level"), ("templates", "message patterns (pattern discovery)"),
                     ("security", "security audit indicators")):
            add(k, w + " (sampled on big files; add --exact)", f"{k} [--exact] [--lines A:B]",
                f'exec {q} stats "$HBMX_FILE" --kind {k} "$@"')
    if lf in ("csv", "tsv", "psv", "jsonl") and e.random_access:
        add("filter", "records matching conditions (col>=x, col=x, col~regex …)", "filter <cond> [cond…] [--fields a,b] [--max N]",
            f'conds=(); while [ $# -gt 0 ] && [[ "$1" != --* ]]; do conds+=(--where "$1"); shift; done\n'
            f'exec {q} filter "$HBMX_FILE" "${{conds[@]}}" "$@"')
        add("record", "one record parsed into fields", "record <line>",
            f'exec {q} record "$HBMX_FILE" --line "${{1:?line}}"')
    if lf in ("jsonl", "json") and shutil.which("jq") and e.random_access:
        add("jq", "a jq expression over the file, first N results (compact)", "jq <expr> [max]",
            (f'jq -c "${{1:?jq expression}}" "$HBMX_FILE" 2>&1 | head -n "${{2:-30}}" | cut -c1-400' if lf == "jsonl" else
             f'jq -c --stream "${{1:?jq expression}}" "$HBMX_FILE" 2>&1 | head -n "${{2:-30}}" | cut -c1-400'))
    if e.fmt == "sqlite":
        add("sql", "read-only SQL query, rows capped", "sql \"<select …>\" [max]",
            'q="${1:?query}"; case "$q" in *[Ll][Ii][Mm][Ii][Tt]*) ;; *) q="$q LIMIT ${2:-50}";; esac\n'
            'exec sqlite3 -readonly -header -separator $\'\\t\' "$HBMX_FILE" "$q"')
    if e.fmt == "zip":
        add("members", "archive entries (name, size), filtered", "members [regex] [max]",
            'unzip -Z1 "$HBMX_FILE" | grep -E "${1:-.}" | head -n "${2:-50}"')
        add("member", "first bytes of one member, as text", "member <name> [bytes]",
            'unzip -p "$HBMX_FILE" "${1:?member}" | head -c "${2:-4000}"')
    if e.fmt == "tar":
        add("members", "archive entries, filtered", "members [regex] [max]",
            'tar -tvf "$HBMX_FILE" | grep -E "${1:-.}" | head -n "${2:-50}"')
        add("member", "first bytes of one member", "member <name> [bytes]",
            'tar -xOf "$HBMX_FILE" "${1:?member}" | head -c "${2:-4000}"')
    if not e.text and not e.codec:
        add("strings", "printable strings (min 6 chars) inside a byte range, with absolute offsets",
            "strings <offset> <length> [max]",
            'tail -c +$(( ${1:?offset} + 1 )) "$HBMX_FILE" | head -c "${2:?length}" | LC_ALL=C strings -a -n 6 -t d '
            '| awk -v o="$1" \'{ $1 = $1 + o; print }\' | head -n "${3:-60}"')
    return T


def write_templates(e: Entry):
    os.makedirs(e.fdir, exist_ok=True)
    reg = [f for f in (e.meta.get("functions") or []) if f.get("by") != "hbmx"]
    names = {f["name"] for f in reg}
    for t in template_functions(e):
        if t["name"] in names:
            continue  # the agent wrote its own under that name: keep it
        p = os.path.join(e.fdir, t["name"])
        write_atomic(p, _fn_script(e, t["name"], t["description"], t["usage"], t["body"]))
        os.chmod(p, 0o755)
        reg.append({"name": t["name"], "path": os.path.relpath(p, e.edir), "lang": "bash", "by": "hbmx",
                    "description": t["description"], "usage": t["usage"], "created": now_iso(), "runs": 0})
    e.meta["functions"] = sorted(reg, key=lambda f: (f.get("by") != "hbmx", f["name"]))


# ---------------------------------------------------------------- probe / open
def identify(path: str, size: int, ext: str) -> dict:
    head = read_at(path, 0, 64 << 10)
    lm = libmagic(path)
    ident: dict = {"libmagic": lm.get("desc", ""), "mime": lm.get("mime", ""), "extension": ext or None}
    if lm.get("probe_tool"):
        ident["probe_tool"] = lm["probe_tool"]
    sig = match_sig(head)
    txt, why = is_text(head)
    if sig and not (sig[0] in ("pe", "bzip2") and txt):
        ident.update({"format": sig[0], "family": sig[1], "text": False, "evidence": [f"magic number of {sig[0]}", why]})
        cls = "structured" if sig[1] in ("database", "columnar", "model", "capture", "archive", "scientific") else "unstructured"
        if sig[1] == "compressed":
            cls = "structured"
        ident["class"] = cls
        return ident
    ident["text"] = txt
    ident["evidence"] = [why]
    if not txt:
        ident.update({"format": "binary", "family": "unknown", "class": "unstructured"})
        ident["evidence"].append("no known signature: mapped by entropy terrain and embedded signatures")
    return ident


def probe(path: str, force: bool = False, ctl: Ctl = NOCTL, count_read: bool = True) -> Tuple[Entry, bool]:
    """Open a file: from memory when its content is known (catalog: same path, size, mtime), else probe and map it.
    Returns (entry, fresh) where fresh is True when this call mapped it. count_read=False (queries from helper
    scripts and the CLI) leaves the read counter and the map file alone."""
    ensure_home()
    rp = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(rp):
        raise HbmxError(f"not a file: {path}")
    stt = os.stat(rp)
    cat = catalog_load()
    rec = cat["files"].get(rp)
    if rec and not force and rec.get("size") == stt.st_size and rec.get("mtime_ns") == stt.st_mtime_ns:
        e = load(rec["sha256"], rp)
        if e and (e.meta.get("hbmx") == VERSION) and e.grid:
            if count_read or not any(l.get("path") == rp for l in e.meta.get("locations") or []):
                touch(e, rp, stt)
            else:
                e.path = rp
            return e, False
    size = stt.st_size
    ext = os.path.splitext(rp)[1].lower().lstrip(".")
    ctl.tick("identifying", 0.0, True)
    ident = identify(rp, size, ext)
    stride = choose_stride(size)
    p = map_pass(rp, size, stride, binary=not ident.get("text"), ctl=ctl)
    sha = p["sha256"]
    known = load(sha, rp)
    if known and known.meta.get("hbmx") == VERSION and known.grid and not force:
        touch(known, rp, stt)  # same content seen at another path or before a touch: memory is still valid
        return known, False
    meta: dict = {"hbmx": VERSION, "sha256": sha, "size": size, "lines": p["lines"] if ident.get("text") else None,
                  "identity": ident, "first_read": now_iso(), "last_read": now_iso(), "reads": 1,
                  "locations": [{"path": rp, "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stt.st_mtime)),
                                 "seen": now_iso()}],
                  "mapping": {"secs": p["secs"], "stride": stride, "cells": len(p["grid"]["nl"])}}
    if known:  # an older hbmx version mapped it: keep what was learnt
        for k in ("first_read", "reads", "extracts", "knowledge"):
            if k in known.meta:
                meta[k] = known.meta[k]
        meta["reads"] = int(meta.get("reads") or 0) + 1
        meta["functions"] = [f for f in known.meta.get("functions") or [] if f.get("by") != "hbmx"]
        meta["landmarks_kept"] = [l for l in known.meta.get("landmarks") or [] if l.get("by") != "hbmx"]
    if rec and rec.get("sha256") and rec["sha256"] != sha:
        meta["lineage"] = {"previous_sha256": rec["sha256"], "previous_size": rec.get("size"),
                           "note": "same path, content changed since the last read"}
    e = Entry(sha, meta, rp)
    grid = dict(p["grid"], stride=stride, size=size, lines=p["lines"], end_nl=p["end_nl"], v=VERSION)
    e._grid = grid
    ctl.tick("structure", 0.0, True)
    try:
        if ident.get("text"):
            head, mid, tail = sample_lines(rp, size)
            dec = lambda L: [l.decode("utf-8", "replace").rstrip("\r") for l in L]  # noqa: E731
            d = detect_text(rp, size, ext, dec(head), dec(mid), dec(tail))
            ident.update({k: d[k] for k in ("class", "format", "confidence") if k in d})
            ident["evidence"] += d.get("evidence", [])
            ident["family"] = "text"
            crlf = read_at(rp, 0, 1 << 16).count(b"\r\n")
            ident["line_ending"] = "crlf" if crlf > 0 else "lf"
            meta["structure"] = d.get("structure") or {}
        else:
            fmt = ident["format"]
            fn = {"gguf": gguf_structure, "safetensors": safetensors_structure, "sqlite": sqlite_structure,
                  "zip": zip_structure, "tar": tar_structure}.get(fmt)
            if fn:
                meta["structure"] = fn(rp)
            elif ident.get("family") == "compressed":
                meta["structure"] = compressed_structure(rp, fmt, size)
                inner = meta["structure"].get("inner") or {}
                ident["evidence"].append(f"decompressed sample is {inner.get('format')}")
            else:
                meta["structure"] = {}
    except Exception as ex:  # a malformed file must still get a map
        meta.setdefault("structure", {})["error"] = f"{type(ex).__name__}: {ex}"
    # timestamps of the cells (the time axis of the map)
    fn = e.ts_fn()
    if fn and e.text and not e.codec:
        ts = [fn(h) if h else None for h in p["heads"]]
        grid["ts"] = ts
        vals = [t for t in ts if t is not None]
        if len(vals) >= 2:
            ok = sum(1 for a, b in zip(vals, vals[1:]) if b >= a - 1)
            grid["ts_sorted"] = ok / (len(vals) - 1) >= 0.98
            meta["structure"]["timestamp"].update({"first": iso(vals[0]), "last": iso(vals[-1]),
                                                   "in_order": grid["ts_sorted"], "span": dur(vals[-1] - vals[0])})
    grid["sigs"] = p["sigs"]
    os.makedirs(e.ddir, exist_ok=True)
    save_json(e, "grid.json", grid)
    build_map(e, p["sigs"] if ident.get("family") == "unknown" else [], ctl)
    for l in meta.pop("landmarks_kept", []) or []:
        l["id"] = f"L{len(meta['landmarks']) + 1}"
        meta["landmarks"].append(l)
    write_templates(e)
    e.save()
    catalog_put(rp, {"sha256": sha, "size": size, "mtime_ns": stt.st_mtime_ns, "format": e.fmt,
                     "class": ident.get("class"), "last_read": now_iso()})
    return e, True


def touch(e: Entry, rp: str, stt):
    m = e.meta
    m["last_read"] = now_iso()
    m["reads"] = int(m.get("reads") or 0) + 1
    locs = m.setdefault("locations", [])
    if not any(l.get("path") == rp for l in locs):
        locs.append({"path": rp, "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stt.st_mtime)), "seen": now_iso()})
    e.path = rp
    e.save()
    catalog_put(rp, {"sha256": e.sha, "size": stt.st_size, "mtime_ns": stt.st_mtime_ns, "format": e.fmt,
                     "class": m["identity"].get("class"), "last_read": now_iso()})


def load(sha: str, path: Optional[str] = None) -> Optional[Entry]:
    try:
        with open(meta_path(sha), encoding="utf-8") as f:
            meta = yload(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or "identity" not in meta:
        return None
    if path is None:
        for loc in reversed(meta.get("locations") or []):
            p = loc.get("path")
            try:
                if p and os.path.getsize(p) == meta.get("size"):
                    path = p
                    break
            except OSError:
                continue
    return Entry(sha, meta, path)


def resolve(arg: str, ctl: Ctl = NOCTL, force: bool = False, count_read: bool = False) -> Entry:
    """A path (probed/opened) or @<sha prefix> (a file already in memory)."""
    arg = str(arg).strip()
    if arg.startswith("@"):
        pre = arg[1:].lower()
        if len(pre) < 6:
            raise HbmxError("@sha needs at least 6 hex digits")
        if len(pre) == 64:
            e = load(pre)
            if e:
                return e
        for path, rec in catalog_load()["files"].items():
            if rec.get("sha256", "").startswith(pre):
                e = load(rec["sha256"])
                if e:
                    return e
        raise HbmxError(f"no file with sha {pre}… in memory")
    return probe(arg, force=force, ctl=ctl, count_read=count_read)[0]


def files(limit: int = 30) -> str:
    cat = catalog_load()["files"]
    rows = sorted(cat.items(), key=lambda kv: kv[1].get("last_read", ""), reverse=True)
    if not rows:
        return f"# hbmx memory ({HOME}) is empty"
    out = [f"# files in hbmx memory · {len(rows)} known · newest first"]
    for p, r in rows[:limit]:
        gone = "" if os.path.exists(p) else "  [gone]"
        out.append(f"@{r.get('sha256', '')[:12]}  {r.get('format', '?'):<12} {human(r.get('size')):>9}  "
                   f"{r.get('last_read', '')[:16]}  {p}{gone}")
    if len(rows) > limit:
        out.append(f"# +{len(rows) - limit} more")
    return "\n".join(out)


# ---------------------------------------------------------------- rendering (map for humans and for the LLM)
def _lr(n) -> str:
    l = n.get("lines")
    b = n.get("bytes")
    s = []
    if l:
        s.append(f"lines {num(l[0])}–{num(l[1])}")
    if b:
        s.append(f"bytes {human(b[0])}–{human(b[1]) if b[1] is not None else 'end'}")
    return " · ".join(s)


def render_identity(e: Entry) -> List[str]:
    m, i = e.meta, e.meta["identity"]
    out = [f"# {e.path or '(memory only)'}",
           f"  {human(e.size)}" + (f" · {num(e.lines)} lines" if e.lines else "") + f" · sha256 {e.sha[:16]}…",
           f"  read {m.get('reads', 1)}× · first {str(m.get('first_read', ''))[:16]} · memory {e.edir}",
           f"identity: {i.get('class')} · {i.get('format')}" + (f" ({i.get('confidence')})" if i.get('confidence') else "")
           + (f" · {i.get('line_ending')}" if i.get("line_ending") else ""),
           f"  libmagic: {clip(i.get('libmagic'), 110)}",
           "  evidence: " + clip("; ".join(i.get("evidence") or []), 220)]
    if i.get("probe_tool"):
        out.append(f"  probe tool: {clip(i['probe_tool'], 200)}")
    if m.get("lineage"):
        out.append(f"  lineage: previous content at this path was @{m['lineage']['previous_sha256'][:12]}")
    return out


def render_structure(e: Entry, full: bool = False) -> List[str]:
    st, out = e.meta.get("structure") or {}, []
    lf, ls = e.line_format, e.line_structure
    if e.codec:
        out.append(f"structure: {e.codec}-compressed → inner {lf}" + (f", ≈{human(st.get('uncompressed'))} inflated"
                                                                         if st.get("uncompressed") else ""))
    if not ls and not st:
        return out
    if ls.get("timestamp"):
        t = ls["timestamp"]
        out.append(f"  time: {t.get('pattern')} via {t.get('via')}{(' ' + str(t.get('field'))) if t.get('field') else ''}"
                   + (f" · {t.get('first')} → {t.get('last')} ({t.get('span')})" if t.get("first") else "")
                   + ("" if t.get("in_order", True) else " · NOT in order"))
    if ls.get("levels_in_sample"):
        out.append("  levels (sample): " + ", ".join(f"{k} {v:.0%}" for k, v in list(ls["levels_in_sample"].items())[:6]))
    if ls.get("columns"):
        cols = ls["columns"]
        out.append(f"  columns ({len(cols)}, delimiter {ls.get('delimiter')!r}, header {'yes' if ls.get('header') else 'no'}):")
        for c in cols[: (60 if full else 16)]:
            out.append(f"    {c['name']:<20} {c['type']:<8} empty {c['empty']:.0%}  e.g. {c['example']}")
        if len(cols) > 16 and not full:
            out.append(f"    … +{len(cols) - 16} columns (map shows all)")
    if ls.get("keys"):
        keys = ls["keys"]
        out.append(f"  keys ({len(keys)}{'+' + str(ls['keys_more']) if ls.get('keys_more') else ''}):")
        for k in keys[: (60 if full else 14)]:
            out.append(f"    {k['key']:<24} {'/'.join(k['types']):<10} {k['present']:.0%}  e.g. {k['example']}")
        if len(keys) > 14 and not full:
            out.append(f"    … +{len(keys) - 14} keys")
    for key in ("fields", "programs_in_sample", "keys_in_sample", "root", "record_tag_guess", "example"):
        if ls.get(key):
            v = ls[key]
            out.append(f"  {key}: " + clip(", ".join(f"{a} {b}" for a, b in v.items()) if isinstance(v, dict) else
                                       ", ".join(map(str, v)) if isinstance(v, list) else v, 200))
    if lf == "json" and ls:
        out.append("  shape: " + clip(json.dumps(ls.get("shape") or ls.get("leaf_paths_seen"), ensure_ascii=False), 400))
    if e.fmt == "gguf":
        md = st.get("metadata", {})
        arch = md.get("general.architecture")
        out.append(f"  gguf v{st.get('version')} · {arch} · {num(st.get('tensors'))} tensors · " +
                   ", ".join(f"{k} {v}" for k, v in md.items() if arch and k.startswith(arch) and not str(v).startswith("[array"))[:300])
        out.append("  tensor types: " + ", ".join(f"{k}×{v}" for k, v in (st.get("tensor_types") or {}).items()))
    elif e.fmt == "safetensors":
        out.append(f"  {st.get('tensors')} tensors · dtypes {st.get('dtypes')}")
    elif e.fmt == "sqlite":
        out.append(f"  {len(st.get('tables', []))} tables/views, {st.get('indexes')} indexes")
    elif e.fmt in ("zip", "tar"):
        out.append(f"  {num(st.get('members') or st.get('members_listed'))} members · "
                   f"{human(st.get('uncompressed') or st.get('content_bytes'))} content · {st.get('extensions')}")
    if st.get("error"):
        out.append(f"  ⚠ structure error: {st['error']}")
    return out


def render_zones(e: Entry, zones: List[dict], title: str) -> List[str]:
    out = [title]
    if not zones:
        return out + ["  (no zones)"]
    if e.text and not e.codec:
        emax = max(z.get("err", 0) for z in zones) or 1
        wmax = max(z.get("warn", 0) for z in zones) or 1
        for z in zones:
            out.append(f"  {z['id']:<6} lines {num(z['lines'][0]):>11}–{num(z['lines'][1]):<11}"
                       f" {(z.get('ts') or '')[:16]:<16} err {spark(z['err'], emax)}{short_num(z['err']):>5}"
                       f" warn {spark(z['warn'], wmax)}{short_num(z['warn']):>5}")
    else:
        for z in zones:
            out.append(f"  {z['id']:<6} {human(z['bytes'][0]):>9}–{human(z['bytes'][1]):<9} entropy {bar(z['entropy'] / 8, 6)}"
                       f" {z['entropy']:.1f} printable {z['printable']:.0%} {z['class']}")
    return out


def render_regions(nodes, depth=0, limit=60) -> List[str]:
    out = []
    for n in nodes or []:
        if len(out) >= limit:
            out.append("  " * (depth + 1) + "…")
            break
        kids = n.get("children") or []
        mark = "▾" if kids else "•"
        out.append(f"{'  ' * (depth + 1)}{mark} {n['id']} {clip(n.get('name'), 60)} · {_lr(n)}"
                   + (f" · {clip(n.get('what'), 120)}" if n.get("what") else ""))
        if kids:
            out += render_regions(kids, depth + 1, limit=40)
    return out


def render_landmarks(e: Entry, limit: int = 40) -> List[str]:
    out = []
    for l in (e.meta.get("landmarks") or [])[:limit]:
        pos = " ".join(x for x in (f"line {num(l['line'])}" if l.get("line") else "",
                                   f"@byte {num(l['byte'])}" if l.get("byte") is not None else "") if x)
        out.append(f"  {l['id']:<4} {clip(l['label'], 70)} · {pos}" + (f" │ {clip(l['text'], 90)}" if l.get("text") else "")
                   + (f"  [{l['by']}]" if l.get("by") not in (None, "hbmx") else ""))
    return out


def render_map(e: Entry, zone: Optional[str] = None, region: Optional[str] = None) -> str:
    """The full map (MAP pane, `map` action). zone/region zoom into one part."""
    if zone:
        zs = zoom_zones(e, zone)
        if not zs:
            raise HbmxError(f"unknown zone {zone}")
        parent = e.find_node(zone)
        return "\n".join(render_zones(e, zs, f"# zone {zone} zoomed · {_lr(parent)} · {len(zs)} sub-zones "
                                             f"(view lines=… or zoom again with the finest stride: {human(e.grid['stride'])})"))
    if region:
        n = e.find_node(region)
        if not n:
            raise HbmxError(f"unknown region {region}")
        return "\n".join([f"# region {region} {n.get('name')} · {_lr(n)}", f"  {n.get('what') or ''}"]
                         + render_regions(n.get("children") or [], 0, 200))
    m = e.meta.get("map") or {}
    out = render_identity(e) + render_structure(e, full=True)
    out.append("regions (index tree):")
    out += render_regions(m.get("regions")) or ["  (none)"]
    out.append("landmarks:")
    out += render_landmarks(e) or ["  (none)"]
    out += render_zones(e, m.get("zones") or [], f"zones ({len(m.get('zones') or [])}, {human(e.grid.get('stride', 0))} cells; "
                                                   "zoom: map zone=zN):")
    fns = e.meta.get("functions") or []
    out.append(f"functions ({e.fdir}):")
    for f in fns:
        out.append(f"  {f['name']:<14} {clip(f.get('description'), 70)} · {f.get('usage')}" + ("" if f.get("by") == "hbmx" else f"  [{f.get('by')}]"))
    import hbmx_know
    out += hbmx_know.render_map(e)
    out.append("howto:")
    out += [f"  - {h}" for h in e.meta.get("howto") or []]
    if e.meta.get("extracts"):
        out.append("extracts:")
        out += [f"  {x['name']}: {x['path']} ({num(x.get('lines'))} lines)" for x in e.meta["extracts"][-8:]]
    return "\n".join(out)


def render_brief(e: Entry, limit: int = 2600) -> str:
    """The compact map the LLM sees every turn."""
    m = e.meta.get("map") or {}
    out = render_identity(e)[:4] + render_structure(e)[:14]
    regs = m.get("regions") or []
    rl = []
    for r in regs[:8]:
        kids = r.get("children") or []
        rl.append(f"{r['id']} {clip(r.get('name'), 30)} ({_lr(r) or clip(r.get('what'), 60)})" + (f" ▸ {len(kids)} children {kids[0]['id']}…{kids[-1]['id']}"
                                                                      f" ({clip(kids[0].get('name'), 20)} … {clip(kids[-1].get('name'), 20)})" if kids else ""))
    out.append("regions: " + ("; ".join(rl) if rl else "(none)"))
    zones = m.get("zones") or []
    if zones:
        out.append(f"zones: {len(zones)} ({zones[0]['id']}–{zones[-1]['id']}, map zone=zN zooms)")
    lms = e.meta.get("landmarks") or []
    if lms:
        out.append("landmarks: " + " · ".join(f"{l['id']} {clip(l['label'], 40)}" + (f" line {num(l['line'])}" if l.get("line") else
                                                                                     f" @{num(l.get('byte'))}") for l in lms[:12]))
    fns = e.meta.get("functions") or []
    out.append("functions: " + ", ".join(f["name"] + ("" if f.get("by") == "hbmx" else "*") for f in fns) + "  (* = written by you)")
    import hbmx_know
    kl = hbmx_know.summary_line(e)
    if kl:
        out.append(kl.replace("🧠 ", "") + "  ([KNOWLEDGE] below, recall for more)")
    out += [f"howto: {h}" for h in (e.meta.get("howto") or [])[:5]]
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit] + "\n… (map action for the rest)"


def locate(sha: str) -> str:
    e = load(sha)
    if not e or not e.path:
        raise HbmxError(f"no copy of {sha[:12]} on disk")
    return e.path


# ---------------------------------------------------------------- CLI
def main(argv=None):
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    core_cmds = {"probe", "brief", "where", "map", "files", "locate", "knowledge", "recall"}
    if argv and argv[0] not in core_cmds and argv[0] not in ("-h", "--help"):
        sys.path.insert(0, os.path.dirname(CORE))
        import hbmx_ops
        return hbmx_ops.main(argv)
    p = argparse.ArgumentParser(prog="hbmx_core.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("probe", help="identify + map a file (instant when known)")
    s.add_argument("file")
    s.add_argument("--force", action="store_true", help="map again even when the file is known")
    for name in ("brief", "where"):
        sub.add_parser(name).add_argument("file")
    s = sub.add_parser("map")
    s.add_argument("file")
    s.add_argument("--zone")
    s.add_argument("--region")
    sub.add_parser("files")
    sub.add_parser("locate").add_argument("sha")
    s = sub.add_parser("knowledge", help="findings + questions answered on a file (or one record: --id K3 / Q2)")
    s.add_argument("file")
    s.add_argument("--id")
    s = sub.add_parser("recall", help="search a file's knowledge")
    s.add_argument("file")
    s.add_argument("query")
    a = p.parse_args(argv)

    def prog(phase, frac):
        sys.stderr.write(f"\r  {phase:<60}")
        sys.stderr.flush()
    try:
        if a.cmd == "files":
            ensure_home()
            print(files(200))
        elif a.cmd == "locate":
            print(locate(a.sha))
        elif a.cmd == "probe":
            e, fresh = probe(a.file, force=a.force, ctl=Ctl(prog if sys.stderr.isatty() else None))
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 70 + "\r")
            print(("# mapped now (" + str(e.meta.get("mapping", {}).get("secs")) + "s)\n" if fresh else "# known file (memory)\n")
                  + render_brief(e, 6000))
        else:
            e = resolve(a.file)
            if a.cmd == "brief":
                print(render_brief(e, 6000))
            elif a.cmd == "where":
                print(f"memory: {e.edir}\nmap: {e.meta_file}\nfunctions: {e.fdir}\ndata: {e.ddir}")
            elif a.cmd == "map":
                print(render_map(e, zone=a.zone, region=a.region))
            elif a.cmd in ("knowledge", "recall"):
                import hbmx_know
                print(hbmx_know.recall(e, a.query) if a.cmd == "recall" else
                      hbmx_know.render_record(e, a.id) if a.id else hbmx_know.render_all(e))
    except (HbmxError, Cancelled) as ex:
        print(f"hbmx: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
