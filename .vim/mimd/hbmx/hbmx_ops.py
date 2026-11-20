#!/usr/bin/env python
"""
hbmx_ops — queries over a mapped file (no LLM). Every result is a bounded, structured text: a "# ..." header that
states what was read and how (exact or sampled), rows with line/byte coordinates, and a "# next: ..." hint when
there is more. Nothing returns raw content in bulk; big outputs go to files (extract) and are mapped in turn.

  view     lines (line / region / time range) or bytes (hex)        seek    line ↔ byte ↔ time
  record   one record parsed into fields (csv, jsonl, logs)         search  ripgrep, bounded, region-aware, counted
  filter   records matching field conditions (csv/jsonl/logs)       stats   levels timeline columns keys templates
  extract  matching lines / ranges written to a new file                    security top
  inflate  decompress a compressed file into memory (random access) functions / run / write_function / note
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbmx_core as C  # noqa: E402
from hbmx_core import Ctl, Entry, HbmxError, NOCTL, clip, human, num, show_line  # noqa: E402

MAX_VIEW = 200
COMPRESSED_SAMPLE = 64 << 20


# ---------------------------------------------------------------- line iteration (exact or sampled)
class LineScan:
    """(line_no, byte_offset, raw_line) over a line range. Exact reads every line; sample reads up to `blocks` cells
    spread evenly over the range (line numbers stay exact: cells start at indexed positions)."""

    def __init__(self, e: Entry, lines=None, exact: Optional[bool] = None, ctl: Ctl = NOCTL, blocks: int = 64):
        e.require_file()
        if not e.text and not e.codec:
            raise HbmxError(f"{e.fmt} is binary: no lines to scan (use functions, view byte=…)")
        self.e, self.ctl, self.blocks = e, ctl, blocks
        if e.codec:
            if lines not in (None, "", "all"):
                raise HbmxError("compressed: line ranges need random access ('inflate' first); whole stream only")
            self.a, self.b, self.start, self.end = 1, None, 0, None
            self.exact = bool(exact)
            self.mode = "exact, whole decompressed stream" if self.exact else \
                f"sample: first {human(COMPRESSED_SAMPLE)} of the decompressed stream (exact=true for all)"
            self.coverage = None
            return
        self.a, self.b = e.line_range(lines)
        self.start = e.line_offset(self.a) or 0
        nxt = e.line_offset(self.b + 1)
        self.end = nxt if nxt is not None else e.size
        nbytes = max(1, self.end - self.start)
        self.exact = exact if exact is not None else nbytes <= C.EXACT_AUTO
        stride = e.grid["stride"]
        self.cells = list(range(self.start // stride, (self.end - 1) // stride + 1))
        if not self.exact and len(self.cells) <= blocks:
            self.exact = True
        if self.exact:
            self.coverage = 1.0
            self.mode = f"exact over lines {num(self.a)}–{num(self.b)} ({human(nbytes)})"
        else:
            k = len(self.cells)
            self.pick = sorted({self.cells[i * k // blocks] for i in range(blocks)})
            self.coverage = min(1.0, len(self.pick) * stride / nbytes)
            self.mode = (f"SAMPLE of {len(self.pick)}/{k} cells ≈{self.coverage:.1%} of lines {num(self.a)}–{num(self.b)} "
                         f"({human(nbytes)}); counts marked ≈ are scaled; exact=true reads all")

    def scale(self, n) -> str:
        if self.exact or not self.coverage:
            return num(n)
        return "≈" + num(round(n / self.coverage))

    def __iter__(self) -> Iterator[Tuple[int, int, bytes]]:
        e = self.e
        if e.codec:
            yield from self._stream()
            return
        if self.exact:
            yield from self._range(self.start, self.end, self.a)
            return
        g, stride = e.grid, e.grid["stride"]
        for n, ci in enumerate(self.pick):
            self.ctl.tick(f"sampling {n * 100 // len(self.pick)}%", n / len(self.pick))
            lo = max(ci * stride, self.start)
            hi = min((ci + 1) * stride, self.end)
            with open(e.path, "rb") as f:
                f.seek(lo)
                buf = f.read(hi - lo + (256 << 10))
            line_no = g["nl_before"][ci] + 1 if lo == ci * stride else e.byte_line(lo)
            p = 0
            if lo > 0 and lo != self.start:
                with open(e.path, "rb") as f:
                    f.seek(lo - 1)
                    at_start = f.read(1) == b"\n"
                if not at_start:
                    q = buf.find(b"\n")
                    if q < 0:
                        continue
                    p, line_no = q + 1, line_no + 1
            while p < hi - lo:
                q = buf.find(b"\n", p)
                raw = buf[p:] if q < 0 else buf[p:q + 1]
                if self.a <= line_no <= self.b:
                    yield line_no, lo + p, raw
                if q < 0:
                    break
                p, line_no = q + 1, line_no + 1

    def _range(self, start: int, end: int, line_no: int):
        total = max(1, end - start)
        pos = start
        with open(self.e.path, "rb") as f:
            f.seek(start)
            for n, raw in enumerate(f):
                if pos >= end:
                    break
                yield line_no, pos, raw
                pos += len(raw)
                line_no += 1
                if not n & 0xFFFF:
                    self.ctl.tick(f"reading {(pos - start) * 100 // total}%", (pos - start) / total)

    def _stream(self):
        cmd = C.DECOMP[self.e.codec]
        p = subprocess.Popen(cmd + [self.e.path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pos, line_no = 0, 1
        try:
            for raw in p.stdout:
                yield line_no, pos, raw
                pos += len(raw)
                line_no += 1
                if not line_no & 0xFFFF:
                    self.ctl.tick(f"streaming {human(pos)}", 0.0)
                if not self.exact and pos >= COMPRESSED_SAMPLE:
                    break
        finally:
            p.kill()
            p.wait()


# ---------------------------------------------------------------- records
def parse_record(e: Entry, raw: bytes) -> Dict[str, object]:
    """One line as fields, by the file's (line) format. `line` is always the whole line text."""
    s = raw.decode("utf-8", "replace").rstrip("\r\n")
    lf, st = e.line_format, e.line_structure
    rec: Dict[str, object] = {}
    if lf in ("csv", "tsv", "psv"):
        try:
            row = next(csv.reader([s], delimiter=st.get("delimiter", ",")))
        except (csv.Error, StopIteration):
            row = []
        names = [c["name"] for c in st.get("columns") or []]
        for i, v in enumerate(row):
            rec[names[i] if i < len(names) else f"col{i + 1}"] = v
    elif lf == "jsonl":
        try:
            obj = json.loads(s)
            rec.update(C.flatten(obj, depth=3) if isinstance(obj, dict) else {"value": obj})
        except ValueError:
            rec["_error"] = "not JSON"
    elif lf == "log.clf":
        m = C.CLF_RX.match(s)
        if m:
            rec.update({k: v for k, v in zip(C.CLF_FIELDS, m.groups()) if v is not None})
            rec["status_class"] = rec.get("status", "?")[:1] + "xx"
    elif lf == "log.syslog":
        m = C.SYSLOG_RX.match(s)
        if m:
            rec.update(dict(zip(("ts", "host", "program", "pid", "msg"), m.groups())))
    if lf in ("log.logfmt", "logfmt"):
        for k, v in C.LOGFMT_RX.findall(s):
            rec[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    if lf.startswith("log.") and lf not in ("log.clf",):
        rx = C.TS_PATTERNS.get((st.get("timestamp") or {}).get("pattern", ""))
        m = rx.search(s[:100]) if rx else None
        if m:
            rec.setdefault("ts", m.group(0).strip("[]"))
        lv = C.LEVEL_RX.search(s[:160])
        if lv:
            rec.setdefault("level", C.norm_level(lv.group(1)))
        if "msg" not in rec:
            rest = s[lv.end():] if lv else (s[m.end():] if m else s)
            rec["msg"] = rest.strip(" :-]|")
    rec["line"] = s
    return rec


def level_of(e: Entry, raw: bytes, rec: Optional[dict] = None) -> str:
    lf, st = e.line_format, e.line_structure
    if lf == "log.clf":
        m = C.CLF_RX.match(raw.decode("utf-8", "replace"))
        return (m.group(8)[:1] + "xx") if m else "(unparsed)"
    if lf == "jsonl" or lf in ("log.logfmt", "logfmt"):
        rec = rec or parse_record(e, raw)
        for k in (st.get("level_key"), "level", "lvl", "severity", "loglevel", "levelname"):
            if k and rec.get(k) not in (None, ""):
                return C.norm_level(rec[k])
        return "(none)"
    m = LEVEL_RXB.search(raw, 0, 160)
    return C.norm_level(m.group(1).decode()) if m else "(none)"


LEVEL_RXB = re.compile(C.LEVEL_RX.pattern.encode())
COND_RX = re.compile(r"^\s*([^\s<>=!~^*]+)\s*(>=|<=|!=|==|=|>|<|!~|~|\^=|\*=)\s*(.*?)\s*$")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compile_where(where) -> List[Tuple[str, str, str, Optional[re.Pattern]]]:
    if isinstance(where, str):
        where = [w for w in re.split(r"\s*(?:&&|\band\b)\s*", where) if w.strip()]
    out = []
    for w in where or []:
        m = COND_RX.match(str(w))
        if not m:
            raise HbmxError(f"bad condition {w!r}: use field<op>value with op one of = != > >= < <= ~ !~ ^= *=")
        f, op, v = m.groups()
        v = v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v
        rx = None
        if op in ("~", "!~"):
            try:
                rx = re.compile(v)
            except re.error as ex:
                raise HbmxError(f"bad regex in {w!r}: {ex}")
        out.append((f, op, v, rx))
    return out


def match_where(rec: dict, conds) -> bool:
    for f, op, v, rx in conds:
        if f.startswith("#"):
            vals = list(rec.values())
            i = int(f[1:]) - 1 if f[1:].isdigit() else -1
            x = vals[i] if 0 <= i < len(vals) else None
        else:
            x = rec.get(f)
        if x is None:
            if op == "!=":
                continue
            return False
        xs = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        if op in ("=", "=="):
            a, b = _num(x), _num(v)
            ok = (a == b) if a is not None and b is not None else xs == v
        elif op == "!=":
            a, b = _num(x), _num(v)
            ok = (a != b) if a is not None and b is not None else xs != v
        elif op in (">", ">=", "<", "<="):
            a, b = _num(x), _num(v)
            if a is None or b is None:
                ta, tb = C.parse_ts_text(xs), C.parse_ts_text(v)
                a, b = (ta, tb) if ta is not None and tb is not None else (xs, v)
            ok = {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        elif op == "~":
            ok = bool(rx.search(xs))
        elif op == "!~":
            ok = not rx.search(xs)
        elif op == "^=":
            ok = xs.startswith(v)
        else:
            ok = v.lower() in xs.lower()
        if not ok:
            return False
    return True


# ---------------------------------------------------------------- views
def _numbered(rows: List[Tuple[int, bytes]], width: int) -> List[str]:
    w = len(num(rows[-1][0])) if rows else 1
    return [f"{num(n):>{w}}│{show_line(raw, width)}" for n, raw in rows]


def read_lines(e: Entry, start: int, count: int) -> List[Tuple[int, bytes]]:
    rows = []
    if e.codec:
        if start > 2_000_000:
            raise HbmxError("compressed: line views stream from the start; past line 2,000,000 'inflate' first")
        for n, _, raw in LineScan(e, exact=True):
            if n >= start:
                rows.append((n, raw))
                if len(rows) >= count:
                    break
        return rows
    off = e.line_offset(start)
    if off is None:
        return rows
    with open(e.path, "rb") as f:
        f.seek(off)
        for k in range(count):
            raw = f.readline(4 << 20)
            if not raw:
                break
            if len(raw) == 4 << 20 and not raw.endswith(b"\n"):  # giant line: skip its rest
                while True:
                    more = f.readline(4 << 20)
                    if not more or more.endswith(b"\n"):
                        break
            rows.append((start + k, raw))
    return rows


def where_is(e: Entry, line: int) -> str:
    if not (e.grid and e.random_access):
        return ""
    zs = (e.meta.get("map") or {}).get("zones") or []
    z = next((z for z in zs if z.get("lines") and z["lines"][0] <= line <= z["lines"][1]), None)

    def deepest(nodes, best=None):
        for n in nodes or []:
            if n.get("lines") and n["lines"][0] <= line <= n["lines"][1]:
                best = deepest(n.get("children"), n)
        return best
    r = deepest((e.meta.get("map") or {}).get("regions"))
    return " · ".join(x for x in (f"zone {z['id']}" if z else "", f"region {r['id']} {clip(r.get('name'), 30)}" if r else "") if x)


def view(e: Entry, line: Optional[int] = None, count: int = 40, byte: Optional[int] = None, length: int = 256,
         region: Optional[str] = None, time_range: Optional[str] = None, width: int = 240, ctl: Ctl = NOCTL) -> str:
    count = max(1, min(int(count or 40), MAX_VIEW))
    if byte is not None:
        return hexview(e, int(byte), int(length or 256))
    e.require_lines()
    if time_range:
        return view_time(e, time_range, count, width, ctl)
    head = ""
    if region:
        node = e.find_node(region) if not str(region).startswith("L") else None
        lm = e.landmark(region) if str(region).startswith("L") else None
        if lm:
            if not lm.get("line"):
                return hexview(e, max(0, int(lm.get("byte") or 0) - 64), length)
            line = max(1, lm["line"] - 3)
            head = f" · landmark {region} {clip(lm['label'], 50)}"
        elif node and node.get("lines"):
            line = node["lines"][0]
            head = f" · {region} {clip(node.get('name'), 40)} spans lines {num(node['lines'][0])}–{num(node['lines'][1])}"
        elif node and node.get("bytes"):
            return hexview(e, node["bytes"][0], length)
        else:
            raise HbmxError(f"unknown region/zone/landmark {region}")
    line = max(1, int(line or 1))
    if line > e.lines:
        raise HbmxError(f"line {num(line)} is past the end ({num(e.lines)} lines)")
    rows = read_lines(e, line, count)
    if not rows:
        return f"# no lines at {line}"
    last = rows[-1][0]
    off = e.line_offset(line) if e.random_access else None
    pos = f" · @byte {num(off)}" if off is not None else ""
    out = [f"# view lines {num(line)}–{num(last)} of {num(e.lines)}{pos} · {e.name}{head}"
           + (f" · {where_is(e, line)}" if where_is(e, line) else "")]
    out += _numbered(rows, width)
    if last < e.lines:
        out.append(f"# next: view line={last + 1} count={count}")
    return "\n".join(out)


def hexview(e: Entry, off: int, length: int = 256) -> str:
    e.require_file()
    length = max(16, min(int(length), 4096))
    off = max(0, min(off, max(0, e.size - 1)))
    data = C.read_at(e.path, off, length)
    out = [f"# view bytes {num(off)}–{num(off + len(data))} of {num(e.size)} (hex) · {e.name}"]
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(chunk[j:j + 2].hex() for j in range(0, len(chunk), 2))
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off + i:010x}: {hx:<39}  {asc}")
    if off + len(data) < e.size:
        out.append(f"# next: view byte={off + len(data)} length={length}")
    return "\n".join(out)


def view_time(e: Entry, spec: str, count: int, width: int, ctl: Ctl) -> str:
    a_s, _, b_s = str(spec).partition("..")
    ta = C.parse_ts_text(a_s, e.year())
    tb = C.parse_ts_text(b_s, e.year()) if b_s.strip() else None
    if ta is None:
        raise HbmxError(f"cannot read the time {a_s!r} (use 2026-01-02T10:00 or the file's own format)")
    la = seek_time_line(e, ta, ctl)
    if la is None:
        return f"# nothing at or after {C.iso(ta)}"
    lb = (seek_time_line(e, tb, ctl) or e.lines + 1) - 1 if tb is not None else None
    rows = read_lines(e, la, count if lb is None else min(count, max(0, lb - la + 1)))
    rng = f"lines {num(la)}–{num(lb)} ({num(lb - la + 1)} lines)" if lb is not None else f"from line {num(la)}"
    out = [f"# view time {C.iso(ta)}" + (f" .. {C.iso(tb)}" if tb else "") + f" → {rng} · showing {len(rows)} · {e.name}"]
    if lb is not None and lb < la:
        return out[0] + "\n# (no lines in that time range)"
    out += _numbered(rows, width)
    if rows and (lb is None or rows[-1][0] < lb):
        out.append(f"# next: view line={rows[-1][0] + 1} count={count}")
    return "\n".join(out)


def seek_time_line(e: Entry, t: float, ctl: Ctl = NOCTL) -> Optional[int]:
    """First line whose timestamp is >= t (time-ordered files), from the cell timestamps + one cell scan."""
    e.require_lines()
    fn = e.ts_fn()
    if not fn:
        raise HbmxError("this file has no timestamp layout (no time axis)")
    g = e.grid
    if not g.get("ts_sorted"):
        raise HbmxError("timestamps are not in order: seek by time is not reliable (use search/filter on the time)")
    pts = [(i, x) for i, x in enumerate(g.get("ts") or []) if x is not None]
    import bisect
    k = bisect.bisect_left([x for _, x in pts], t)
    ci = pts[k - 1][0] if k > 0 else 0
    start_line = g["nl_before"][ci] + 1 if ci > 0 else 1
    off = e.line_offset(start_line) or 0
    scanned = 0
    line = start_line
    with open(e.path, "rb") as f:
        f.seek(off)
        for raw in f:
            x = fn(raw)
            if x is not None and x >= t:
                return line
            line += 1
            scanned += len(raw)
            if not line & 0x3FFF:
                ctl.tick("seeking", 0.0)
            if scanned > 256 << 20:
                break
    return None


def seek(e: Entry, line=None, byte=None, time_=None, ctl: Ctl = NOCTL) -> str:
    e.require_lines()
    if time_ is not None:
        t = C.parse_ts_text(time_, e.year())
        if t is None:
            raise HbmxError(f"cannot read the time {time_!r}")
        L = seek_time_line(e, t, ctl)
        if L is None:
            return f"# seek time {C.iso(t)}: after the last line (last time {e.line_structure.get('timestamp', {}).get('last')})"
        head = f"# seek time {C.iso(t)} → first line at or after it: line {num(L)}"
    elif byte is not None:
        L = e.byte_line(int(byte))
        head = f"# seek byte {num(byte)} → inside line {num(L)}"
    else:
        L = max(1, min(int(line or 1), e.lines))
        head = f"# seek line {num(L)}"
    off = e.line_offset(L)
    rows = read_lines(e, max(1, L - 1), 4 if L > 1 else 3)
    out = [head + f" @byte {num(off)}" + (f" · {where_is(e, L)}" if where_is(e, L) else "")]
    for n, raw in rows:
        out.append(f"{'▶' if n == L else ' '}{num(n)}│{show_line(raw, 240)}")
    return "\n".join(out)


def record(e: Entry, line: int) -> str:
    e.require_lines()
    rows = read_lines(e, int(line), 1)
    if not rows:
        raise HbmxError(f"no line {line}")
    rec = parse_record(e, rows[0][1])
    out = [f"# record line {num(line)} · {e.line_format} · {e.name}"]
    for k, v in rec.items():
        if k == "line":
            continue
        out.append(f"  {k}: {clip(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), 300)}")
    if len(rec) <= 1:
        out.append("  (no fields recognised for this format)")
    out.append(f"  _raw: {show_line(rows[0][1], 300)}")
    return "\n".join(out)


# ---------------------------------------------------------------- search (ripgrep)
def _feed(proc, path: str, start: int, end: int):
    try:
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start
            while left > 0:
                buf = f.read(min(left, 1 << 20))
                if not buf:
                    break
                proc.stdin.write(buf)
                left -= len(buf)
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def _rg(e: Entry, args: List[str], start: int, end: Optional[int], ctl: Ctl, timeout: float = 600) -> List[str]:
    """Run ripgrep on the whole file, or on a byte range fed through stdin. Returns stdout lines."""
    whole = end is None or (start == 0 and end >= e.size)
    cmd = ["rg", "-a", "--no-filename", "--no-heading", "--color", "never"] + args + (["-z"] if e.codec else []) + \
          ([e.path] if whole else ["-"])
    p = subprocess.Popen(cmd, stdin=None if whole else subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if not whole:
        threading.Thread(target=_feed, args=(p, e.path, start, end), daemon=True).start()
    out: List[bytes] = []
    reader = threading.Thread(target=lambda: out.extend(p.stdout), daemon=True)
    reader.start()
    t0 = time.time()
    try:
        while p.poll() is None:
            ctl.tick(f"rg {time.time() - t0:.0f}s", 0.0)
            if time.time() - t0 > timeout:
                raise HbmxError(f"search took longer than {timeout:.0f}s (narrow it with lines=…)")
            time.sleep(0.05)
    except BaseException:
        p.kill()
        raise
    reader.join(5)
    if p.returncode not in (0, 1):
        raise HbmxError("rg: " + p.stderr.read().decode("utf-8", "replace").strip()[:400])
    return [b.decode("utf-8", "replace").rstrip("\n") for b in out]


def search(e: Entry, pattern: str, regex: bool = True, icase: bool = False, max_hits: int = 30, lines=None,
           count: Optional[bool] = None, after_line: Optional[int] = None, width: int = 240, ctl: Ctl = NOCTL) -> str:
    e.require_file()
    if not pattern:
        raise HbmxError("search needs a pattern")
    max_hits = max(1, min(int(max_hits or 30), MAX_VIEW))
    if after_line:
        lines = f"{int(after_line) + 1}:"
    if e.codec:
        if lines:
            raise HbmxError("compressed: no line ranges ('inflate' first)")
        a, start, end, nbytes = 1, 0, None, e.size
    elif e.text:
        a, b = e.line_range(lines)
        start = e.line_offset(a) or 0
        nxt = e.line_offset(b + 1)
        end = nxt if nxt is not None else e.size
        nbytes = end - start
    else:
        a, start, end, nbytes = 1, 0, None, e.size
    if count is None:
        count = nbytes <= 1 << 30
    flags = (["-F"] if not regex else []) + (["-i"] if icase else [])
    if not shutil.which("rg"):
        return _search_py(e, pattern, regex, icase, max_hits, lines, count, width, ctl)
    raw = _rg(e, ["-n", "-b", "-m", str(max_hits), "--max-columns", str(width + 200), "--max-columns-preview",
                  *flags, "-e", pattern], start, end, ctl)
    hits = []
    for l in raw:
        m = re.match(r"^(\d+):(\d+):(.*)$", l)
        if m:
            hits.append((int(m.group(1)) + a - 1, int(m.group(2)) + start, m.group(3)))
    total = None
    if count:
        c = _rg(e, ["-c", *flags, "-e", pattern], start, end, ctl)
        total = sum(int(x) for x in c if x.strip().isdigit()) if c else 0
    scope = f"lines {num(a)}–{num(e.line_range(lines)[1])}" if e.text and not e.codec else "whole file"
    head = (f"# search /{clip(pattern, 80)}/{'i' if icase else ''}{'' if regex else ' (fixed string)'} · {e.name} {scope}"
            f" · showing {len(hits)}" + (f" of {num(total)} matching lines" if total is not None else
                                         (" (limit reached, more may exist; count=true to count)" if len(hits) >= max_hits else "")))
    out = [head]
    w = len(num(hits[-1][0])) if hits else 1
    for n, off, text in hits:
        out.append(f"line {num(n):>{w}} @{num(off)} │ {show_line(text.encode(), width)}")
    if not hits:
        out.append("  (no match)")
    elif (total is None and len(hits) >= max_hits) or (total is not None and total > len(hits)):
        out.append(f"# next: search … after_line={hits[-1][0]}  ·  all of them into a file: extract pattern=…")
    return "\n".join(out)


def _search_py(e, pattern, regex, icase, max_hits, lines, count, width, ctl):
    rx = re.compile(pattern if regex else re.escape(pattern), re.I if icase else 0)
    sc = LineScan(e, lines, exact=True, ctl=ctl)
    hits, total = [], 0
    for n, off, raw in sc:
        if rx.search(raw.decode("utf-8", "replace")):
            total += 1
            if len(hits) < max_hits:
                hits.append(f"line {num(n)} @{num(off)} │ {show_line(raw, width)}")
            elif not count:
                break
    return "\n".join([f"# search /{pattern}/ (python, rg not found) · showing {len(hits)}"
                      + (f" of {num(total)}" if count else "")] + (hits or ["  (no match)"]))


# ---------------------------------------------------------------- filter
def filter_records(e: Entry, where, fields=None, max_hits: int = 30, lines=None, count: bool = False,
                   width: int = 200, ctl: Ctl = NOCTL) -> str:
    conds = compile_where(where)
    if not conds:
        raise HbmxError("filter needs at least one condition in where")
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    max_hits = max(1, min(int(max_hits or 30), MAX_VIEW))
    sc = LineScan(e, lines, exact=True, ctl=ctl)
    shown, total, seen = [], 0, 0
    for n, off, raw in sc:
        seen += 1
        rec = parse_record(e, raw)
        if not match_where(rec, conds):
            continue
        total += 1
        if len(shown) < max_hits:
            if fields:
                shown.append(f"line {num(n)} │ " + "  ".join(f"{f}={clip(rec.get(f), 60)}" for f in fields))
            else:
                shown.append(f"line {num(n)} │ {show_line(raw, width)}")
        elif not count:
            break
    cond_s = " and ".join(f"{f}{op}{v}" for f, op, v, _ in conds)
    head = f"# filter {cond_s} · {e.name} · read {num(seen)} records · showing {len(shown)}" + \
           (f" of {num(total)} matching" if count else (" (limit reached; count=true counts all)" if len(shown) >= max_hits else ""))
    out = [head] + (shown or ["  (no record matches)"])
    if shown and len(shown) >= max_hits:
        last = int(shown[-1].split("│")[0].split()[1].replace(",", ""))
        out.append(f"# next: filter … lines={last + 1}:")
    return "\n".join(out)


# ---------------------------------------------------------------- stats
NICE_BUCKETS = [1, 5, 10, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400, 30 * 86400]

MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\bhttps?://[^\s\"']+"), "<URL>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b(?:0x)?[0-9a-f]{10,}\b", re.I), "<HEX>"),
    (re.compile(r"(?<![\w<])[-+]?\d+(?:[.,:]\d+)*(?:ms|s|us|µs|kb|mb|gb|b|%)?(?![\w>])", re.I), "<N>"),
]
MSG_KEYS = ("msg", "message", "event", "log", "text", "error", "err")

SECURITY = [
    ("private_key", "critical", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY", True),
    ("aws_access_key", "critical", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", True),
    ("credential_in_clear", "high", r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)\b[\"']?\s*[=:]\s*[\"']?[^\s\"',;&]{6,}", True),
    ("jwt_or_bearer", "high", r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}|(?i:bearer)\s+[\w\-.~+/]{20,}", True),
    ("sql_injection", "high", r"(?i)union(?:\s|%20|\+|/\*\*/)+(?:all(?:\s|%20|\+))?select|'\s*or\s*'?1'?\s*=\s*'?1|\bsleep\(\d+\)|information_schema|xp_cmdshell|%27%20or%20", False),
    ("path_traversal", "high", r"(?i)\.\./\.\./|%2e%2e%2f|\.\.%2f|/etc/passwd|/etc/shadow|c:\\windows\\", False),
    ("command_injection", "high", r"(?i)(?:;|\||`|\$\(|%3b|%7c)\s*(?:wget|curl|nc|ncat|bash|sh|python\d?|perl|chmod|rm)\b|/bin/(?:ba)?sh\b|cmd\.exe|powershell", False),
    ("auth_failure", "medium", r"(?i)failed password|authentication fail|invalid user|login fail|failed login|unauthori[sz]ed|access denied|permission denied|bad password", False),
    ("privilege_use", "medium", r"(?i)\bsudo\b.*COMMAND=|session opened for user root|\bsu\b\[\d+\]|became root|setuid", False),
    ("xss", "medium", r"(?i)<script|javascript:|onerror\s*=|onload\s*=|%3cscript", False),
    ("scanner_tool", "low", r"(?i)sqlmap|nikto|nmap|masscan|zgrab|gobuster|dirbuster|wpscan|nuclei|acunetix|hydra", False),
    ("sensitive_path", "low", r"(?i)/\.env\b|/\.git/|wp-login|wp-admin|phpmyadmin|/\.aws/|/\.ssh/|/server-status|/actuator", False),
]
_SEC = [(n, s, re.compile(p), secret) for n, s, p, secret in SECURITY]
# prefilter: one case-insensitive alternation (a superset); the precise per-indicator regexes run only on its hits
_SEC_ANY = re.compile("|".join(f"(?:{p.replace('(?i)', '').replace('(?i:bearer)', 'bearer')})" for _, _, p, _ in SECURITY), re.I)
IP_RX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def mask_secret(s: str) -> str:
    s = s.strip()
    return s[:4] + "…" + f"({len(s)} chars)" if len(s) > 6 else "…"


def template_of(e: Entry, raw: bytes, ts_rx) -> str:
    s = raw.decode("utf-8", "replace").rstrip("\r\n")
    lf = e.line_format
    if lf == "jsonl":
        try:
            o = json.loads(s)
        except ValueError:
            return "(not json)"
        if isinstance(o, dict):
            flat = C.flatten(o)
            msg = next((flat[k] for k in MSG_KEYS if isinstance(flat.get(k), str)), None)
            lv = next((str(flat[k]) for k in ("level", "lvl", "severity") if flat.get(k)), "")
            if msg is not None:
                s = f"{lv.upper()} {msg}".strip()
            else:
                return "{" + ",".join(sorted(flat.keys()))[:160] + "}"
    elif lf == "log.clf":
        m = C.CLF_RX.match(s)
        if m:
            path = m.group(6).split("?")[0]
            s = f"{m.group(5)} {path} {m.group(8)}"
    elif ts_rx is not None:
        m = ts_rx.search(s[:100])
        if m:
            s = s[:m.start()] + s[m.end():]
    for rx, rep in MASKS:
        s = rx.sub(rep, s)
    s = " ".join(s.split())
    return s[:180]


def _bucket(span: float, want: int = 40) -> int:
    for b in NICE_BUCKETS:
        if span / b <= want:
            return b
    return NICE_BUCKETS[-1]


class Filtered:
    """A LineScan restricted to records matching `where` conditions (same mode/coverage, fewer lines)."""

    def __init__(self, e: Entry, sc: LineScan, conds):
        self.e, self.sc, self.conds = e, sc, conds
        self.mode = sc.mode + " · where " + " and ".join(f"{f}{op}{v}" for f, op, v, _ in conds)

    def __getattr__(self, k):
        return getattr(self.sc, k)

    def __iter__(self):
        for ln, off, raw in self.sc:
            if match_where(parse_record(self.e, raw), self.conds):
                yield ln, off, raw


def stats(e: Entry, kind: str, field: Optional[str] = None, regex: Optional[str] = None, exact: Optional[bool] = None,
          lines=None, top: int = 20, where=None, baseline=None, ctl: Ctl = NOCTL) -> str:
    kind = (kind or "").lower().strip()
    if kind in ("overview", "map", "summary"):
        return C.render_map(e)
    conds = compile_where(where) if where else None
    if kind == "timeline":
        return stats_timeline(e, exact, lines, ctl, conds)
    fn = {"levels": stats_levels, "columns": stats_columns, "keys": stats_keys, "templates": stats_templates,
          "patterns": stats_templates, "security": stats_security, "top": stats_top, "compare": stats_compare}.get(kind)
    if not fn:
        raise HbmxError(f"unknown stats kind {kind!r}: levels timeline columns keys templates security top compare overview")
    sc = LineScan(e, lines, exact=exact, ctl=ctl)
    if conds:
        sc = Filtered(e, sc, conds)
    return fn(e, sc, field=field, regex=regex, top=max(3, min(int(top or 20), 60)), baseline=baseline, conds=conds,
              lines=lines)


def stats_levels(e: Entry, sc: LineScan, top=20, **_) -> str:
    cnt, first = Counter(), {}
    n = 0
    for ln, _, raw in sc:
        n += 1
        lv = level_of(e, raw)
        cnt[lv] += 1
        first.setdefault(lv, ln)
    out = [f"# stats levels · {e.name} · {sc.mode} · {num(n)} lines read"]
    if not cnt:
        return out[0] + "\n  (no lines)"
    mx = max(cnt.values())
    order = sorted(cnt, key=lambda k: (k not in C.BAD_LEVELS and not k.startswith("5"), -cnt[k]))
    for k in order[:top]:
        out.append(f"  {k:<10} {sc.scale(cnt[k]):>12} {cnt[k] / n:6.1%} {C.bar(cnt[k] / mx, 12)}  first line {num(first[k])}")
    return "\n".join(out)


def stats_timeline(e: Entry, exact, lines, ctl, conds=None) -> str:
    g = e.grid
    st = e.line_structure.get("timestamp")
    if not st:
        raise HbmxError("no timestamps in this file: no timeline")
    if not exact and not conds and g.get("ts") and g.get("ts_sorted") and not e.codec:
        a, b = e.line_range(lines)
        stride = g["stride"]
        cells = [i for i in range(len(g["nl"])) if g["nl_before"][i] + 1 <= b and
                 (i + 1 >= len(g["nl"]) or g["nl_before"][i + 1] + 1 >= a)]
        pts = [(i, g["ts"][i]) for i in cells if g["ts"][i] is not None]
        if len(pts) >= 2:
            span = pts[-1][1] - pts[0][1]
            bk = _bucket(span)
            agg = defaultdict(lambda: [0, 0, 0])
            last_t = pts[0][1]
            for i in cells:
                t = g["ts"][i] if g["ts"][i] is not None else last_t
                last_t = t
                k = int(t // bk) * bk
                agg[k][0] += g["nl"][i]
                agg[k][1] += g["err"][i]
                agg[k][2] += g["warn"][i]
            mx = max(v[0] for v in agg.values()) or 1
            emx = max(v[1] for v in agg.values()) or 1
            out = [f"# stats timeline · {e.name} · from the map grid ({len(cells)} cells of {C.human(stride)}; instant, "
                   f"≈ at cell resolution; err/warn = keyword hits; exact=true parses every line) · bucket {C.dur(bk)}",
                   f"  {'time':<17} {'lines':>9} {'':<10} {'err':>7} {'':<4} {'warn':>7}"]
            for k in sorted(agg):
                v = agg[k]
                out.append(f"  {C.iso(k)[:16]:<17} {num(v[0]):>9} {C.bar(v[0] / mx, 10)} {num(v[1]):>7} "
                           f"{C.spark(v[1], emx)*3:<4} {num(v[2]):>7}")
            return "\n".join(out)
    sc = LineScan(e, lines, exact=exact, ctl=ctl)
    if conds:
        sc = Filtered(e, sc, conds)
    fn = e.ts_fn()
    rows: List[Tuple[float, str]] = []
    nots = 0
    for _, _, raw in sc:
        t = fn(raw)
        if t is None:
            nots += 1
            continue
        rows.append((t, level_of(e, raw)))
    if not rows:
        return f"# stats timeline · {e.name} · {sc.mode}\n  (no timestamps parsed)"
    t0, t1 = min(r[0] for r in rows), max(r[0] for r in rows)
    bk = _bucket(t1 - t0)
    agg = defaultdict(lambda: [0, 0, 0])
    for t, lv in rows:
        k = int(t // bk) * bk
        agg[k][0] += 1
        agg[k][1] += lv in C.BAD_LEVELS or lv.startswith("5")
        agg[k][2] += lv in ("WARN", "4xx")
    mx = max(v[0] for v in agg.values()) or 1
    out = [f"# stats timeline · {e.name} · {sc.mode} · bucket {C.dur(bk)} · {num(len(rows))} timed lines"
           + (f", {num(nots)} without time" if nots else ""),
           f"  {'time':<17} {'lines':>9} {'':<10} {'error':>7} {'warn':>7}"]
    for k in sorted(agg):
        v = agg[k]
        out.append(f"  {C.iso(k)[:16]:<17} {sc.scale(v[0]):>9} {C.bar(v[0] / mx, 10)} {sc.scale(v[1]):>7} {sc.scale(v[2]):>7}")
    return "\n".join(out)


def stats_columns(e: Entry, sc: LineScan, top=20, field=None, **_) -> str:
    if e.line_format not in ("csv", "tsv", "psv"):
        raise HbmxError("columns is for csv/tsv files (keys for jsonl)")
    names = [c["name"] for c in e.line_structure.get("columns") or []]
    header = e.line_structure.get("header")
    typ = [Counter() for _ in names]
    vals = [Counter() for _ in names]
    lo, hi = [None] * len(names), [None] * len(names)
    n = 0
    delim = e.line_structure.get("delimiter", ",")
    numeric = [False] * len(names)
    empty = [0] * len(names)
    for ln, _, raw in sc:
        if header and ln == 1:
            continue
        n += 1
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        row = line.split(delim) if '"' not in line else next(csv.reader([line], delimiter=delim), [])
        typing = n <= 20000
        for i in range(len(names)):
            v = row[i] if i < len(row) else ""
            if not v.strip():
                empty[i] += 1
            if typing:
                t = C.guess_type(v)
                typ[i][t] += 1
                if n == 20000:
                    numeric[i] = (max((k for k in typ[i] if k != "empty"), key=typ[i].get, default="") in ("int", "float"))
            vc = vals[i]
            if len(vc) < 5000 or v in vc:
                vc[v] += 1
            if typing or numeric[i]:
                x = _num(v)
                if x is not None:
                    lo[i] = x if lo[i] is None or x < lo[i] else lo[i]
                    hi[i] = x if hi[i] is None or x > hi[i] else hi[i]
    out = [f"# stats columns · {e.name} · {sc.mode} · {num(n)} rows read"]
    for i, name in enumerate(names):
        if field and name != field:
            continue
        nonempty = {k: v for k, v in typ[i].items() if k != "empty"}
        t = max(nonempty, key=nonempty.get) if nonempty else "empty"
        dist = len(vals[i])
        line = (f"  {name:<20} {t:<8} empty {empty[i] / max(1, n):5.1%} distinct "
                f"{('5000+' if dist >= 5000 else num(dist)):>6}")
        if lo[i] is not None:
            line += f" min {lo[i]:g} max {hi[i]:g}"
        if dist <= 50 or field:
            line += " · top " + ", ".join(f"{clip(k, 24)}×{v}" for k, v in vals[i].most_common(5 if not field else top))
        out.append(line)
    return "\n".join(out)


def stats_keys(e: Entry, sc: LineScan, top=20, field=None, **_) -> str:
    if e.line_format != "jsonl":
        raise HbmxError("keys is for json lines (columns for csv)")
    present, types, vals = Counter(), defaultdict(Counter), defaultdict(Counter)
    n = 0
    for _, _, raw in sc:
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        n += 1
        for k, v in C.flatten(o, depth=3).items():
            present[k] += 1
            types[k][C.vtype(v)] += 1
            if not isinstance(v, (dict, list)):
                if len(vals[k]) < 2000 or v in vals[k]:
                    vals[k][v] += 1
    out = [f"# stats keys · {e.name} · {sc.mode} · {num(n)} objects read · {len(present)} key paths"]
    for k, c in present.most_common(80 if not field else None):
        if field and k != field:
            continue
        dist = len(vals[k])
        line = f"  {k:<28} {c / max(1, n):6.1%}  {'/'.join(types[k]):<12} distinct {('2000+' if dist >= 2000 else num(dist)):>6}"
        if dist <= 30 or field:
            line += " · top " + ", ".join(f"{clip(v, 24)}×{x}" for v, x in vals[k].most_common(5 if not field else top))
        out.append(line)
    return "\n".join(out)


def stats_templates(e: Entry, sc: LineScan, top=20, **_) -> str:
    ts_rx = C.TS_PATTERNS.get((e.line_structure.get("timestamp") or {}).get("pattern", ""))
    cnt, first, ex = Counter(), {}, {}
    n, other = 0, 0
    for ln, _, raw in sc:
        n += 1
        t = template_of(e, raw, ts_rx)
        if t in cnt or len(cnt) < 20000:
            cnt[t] += 1
            if t not in first:
                first[t] = ln
        else:
            other += 1
    out = [f"# stats templates (pattern discovery) · {e.name} · {sc.mode} · {num(n)} lines → {num(len(cnt))} patterns"
           + (f" (+{num(other)} lines in overflow)" if other else ""),
           "  <N> number · <IP> · <UUID> · <HEX> · <URL> · <EMAIL> are masked values",
           f"  {'count':>10}  {'%':>6}  {'first':>9}  pattern"]
    for t, c in cnt.most_common(top):
        out.append(f"  {sc.scale(c):>10}  {c / max(1, n):6.1%}  {num(first[t]):>9}  {t}")
    rare = [t for t, c in cnt.items() if c == 1]
    if rare:
        out.append(f"  rare: {num(len(rare))} patterns seen once (outliers), e.g. line {num(first[rare[0]])}: {clip(rare[0], 120)}")
    return "\n".join(out)


def _security_candidates(e: Entry, sc: LineScan) -> Tuple[Iterator[Tuple[int, str]], str]:
    """Lines that may carry an indicator: ripgrep over the whole range (exact, fast) when available,
    else the Python prefilter over the scan (exact or sampled like the scan)."""
    if shutil.which("rg") and (e.random_access or e.codec):
        pat = "|".join(f"(?:{p})" for _, _, p, _ in SECURITY)
        out = _rg(e, ["-n", "-i", "--max-columns", "800", "-e", pat], sc.start, sc.end, sc.ctl)
        a = sc.a if not e.codec else 1

        def it():
            for l in out:
                n, _, text = l.partition(":")
                if n.isdigit():
                    yield int(n) + a - 1, text
        return it(), "exact (ripgrep over the whole range)"

    def it2():
        for ln, _, raw in sc:
            s = raw.decode("utf-8", "replace")
            if _SEC_ANY.search(s):
                yield ln, s
    return it2(), sc.mode


def stats_security(e: Entry, sc: LineScan, top=20, **_) -> str:
    hits: Dict[str, dict] = {}
    ips, fail_ips = Counter(), Counter()
    status = Counter()
    n = 0
    clf = e.line_format == "log.clf"
    if clf:  # traffic picture: cheap byte splits, no regex per line
        for ln, _, raw in sc:
            n += 1
            sp = raw.find(b" ")
            ip = raw[:sp].decode("ascii", "replace")
            ips[ip] += 1
            parts = raw.split(b'"', 3)
            code = parts[2].split(None, 1)[0] if len(parts) > 2 and parts[2].strip() else b"?"
            status[code[:1].decode("ascii", "replace") + "xx"] += 1
            if code in (b"401", b"403"):
                fail_ips[ip] += 1
    cands, ind_mode = _security_candidates(e, sc)
    flagged = 0
    for ln, s in cands:
        flagged += 1
        if not clf:
            for ip in IP_RX.findall(s[:400]):
                ips[ip] += 1
        for name, sev, rx, secret in _SEC:
            m = rx.search(s)
            if not m:
                continue
            h = hits.setdefault(name, {"sev": sev, "n": 0, "refs": [], "vals": Counter()})
            h["n"] += 1
            if len(h["refs"]) < 4:
                h["refs"].append(ln)
            h["vals"][mask_secret(m.group(0)) if secret else clip(m.group(0), 50)] += 1
            if name == "auth_failure":
                for ip in IP_RX.findall(s):
                    fail_ips[ip] += 1
    exact_ind = ind_mode.startswith("exact (rip")
    sc_ = (lambda x: num(x)) if exact_ind else sc.scale
    out = [f"# stats security (audit indicators) · {e.name} · indicators: {ind_mode} · {num(flagged)} flagged lines"
           + (f" · traffic: {sc.mode}" if clf else ""),
           "  severity  indicator             count  first lines        top values (secrets masked)"]
    for name, h in sorted(hits.items(), key=lambda kv: (SEV[kv[1]["sev"]], -kv[1]["n"])):
        refs = " ".join(num(r) for r in h["refs"])
        vals = "; ".join(f"{v}×{c}" for v, c in h["vals"].most_common(3))
        out.append(f"  {h['sev']:<9} {name:<20} {sc_(h['n']):>7}  {refs:<18} {clip(vals, 140)}")
    if not hits:
        out.append("  (no indicator matched)")
    if status:
        out.append("  http status classes: " + ", ".join(f"{k} {sc.scale(v)}" for k, v in sorted(status.items())))
    if fail_ips:
        out.append("  sources of auth failures / 401-403: " + ", ".join(f"{ip}×{c}" for ip, c in fail_ips.most_common(8)))
    if ips:
        out.append(f"  top IPs{'' if clf else ' in flagged lines'} ({num(len(ips))} distinct): " + ", ".join(f"{ip}×{c}" for ip, c in ips.most_common(10)))
    out.append("# next: search pattern=<indicator> to see context · note kind=finding to keep a conclusion")
    return "\n".join(out)


def stats_compare(e: Entry, sc: LineScan, top=20, baseline=None, conds=None, lines=None, **_) -> str:
    """What is different in a part of the file: levels and message patterns over- or under-represented in the
    target range (lines=…) against a baseline (default: a sample of the whole file)."""
    if lines in (None, "", "all"):
        raise HbmxError("compare needs lines=<target> (a zone z7, region r1.2 or A:B); baseline=… optional (default: whole file)")
    base = LineScan(e, baseline, exact=False, ctl=sc.ctl, blocks=48)
    if conds:
        base = Filtered(e, base, conds)
    ts_rx = C.TS_PATTERNS.get((e.line_structure.get("timestamp") or {}).get("pattern", ""))

    def dist(scan):
        tpl, lv, first, n = Counter(), Counter(), {}, 0
        for ln, _, raw in scan:
            n += 1
            t = template_of(e, raw, ts_rx)
            if t in tpl or len(tpl) < 20000:
                tpl[t] += 1
                first.setdefault(t, ln)
            lv[level_of(e, raw)] += 1
        return tpl, lv, first, max(1, n)
    ta, la, fa, na = dist(sc)
    tb, lb, _, nb = dist(base)
    out = [f"# stats compare · {e.name}", f"  target   {sc.mode} · {num(na)} lines",
           f"  baseline {base.mode} · {num(nb)} lines", "  levels (share in target vs baseline, lift ×):"]
    for k in sorted(set(la) | set(lb), key=lambda k: -(la[k] / na)):
        pa, pb = la[k] / na, lb[k] / nb
        lift = pa / max(pb, 0.5 / nb)
        mark = " ▲" if lift >= 2 and la[k] >= 5 else " ▼" if lift <= 0.5 and lb[k] >= 5 else ""
        out.append(f"    {k:<10} {pa:7.2%} vs {pb:7.2%}  ×{lift:5.1f}{mark}")
    rows = []
    for t, c in ta.items():
        if c < 3:
            continue
        pa, pb = c / na, tb.get(t, 0) / nb
        rows.append((pa / max(pb, 0.5 / nb), c, t, pa, pb))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    out.append("  patterns over-represented in the target (lift × = target share / baseline share):")
    for lift, c, t, pa, pb in rows[:top]:
        if lift < 1.5:
            break
        out.append(f"    ×{lift:6.1f} {pa:7.2%} vs {pb:7.2%}  first line {num(fa[t]):>9}  {t}")
    if not rows or rows[0][0] < 1.5:
        out.append("    (nothing stands out: the target looks like the baseline)")
    gone = sorted(((tb[t] / nb, t) for t in tb if tb[t] >= 5 and ta.get(t, 0) == 0), reverse=True)[:5]
    if gone:
        out.append("  patterns of the baseline absent from the target:")
        out += [f"    {pb:7.2%}  {t}" for pb, t in gone]
    return "\n".join(out)


def stats_top(e: Entry, sc: LineScan, field=None, regex=None, top=20, **_) -> str:
    if not field and not regex:
        raise HbmxError("top needs field (a column / key / clf field such as ip, path, status) or regex with a (group)")
    rx = re.compile(regex) if regex else None
    cnt = Counter()
    n = miss = 0
    for _, _, raw in sc:
        n += 1
        if rx:
            m = rx.search(raw.decode("utf-8", "replace"))
            v = (m.group(1) if m and m.groups() else m.group(0)) if m else None
        else:
            v = parse_record(e, raw).get(field)
        if v is None or v == "":
            miss += 1
            continue
        v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if v in cnt or len(cnt) < 50000:
            cnt[v] += 1
    what = f"field {field}" if field else f"regex /{regex}/"
    out = [f"# stats top {what} · {e.name} · {sc.mode} · {num(n)} lines, {num(len(cnt))}{'+' if len(cnt) >= 50000 else ''} "
           f"distinct values, {num(miss)} without it"]
    mx = max(cnt.values()) if cnt else 1
    for v, c in cnt.most_common(top):
        out.append(f"  {sc.scale(c):>10} {c / max(1, n):6.1%} {C.bar(c / mx, 10)} {clip(v, 120)}")
    return "\n".join(out)


# ---------------------------------------------------------------- extract / inflate
def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name or "")[:60].strip("._") or time.strftime("x%Y%m%d-%H%M%S")


def extract(e: Entry, name: Optional[str] = None, pattern: Optional[str] = None, regex: bool = True, icase: bool = False,
            lines=None, time_range: Optional[str] = None, where=None, ctl: Ctl = NOCTL) -> str:
    """Write matching lines / a range to a new file in the file's memory; return where and how much (never the content)."""
    e.require_file()
    outdir = os.path.join(e.ddir, "extract")
    os.makedirs(outdir, exist_ok=True)
    name = _safe_name(name or (pattern and f"match-{pattern}") or (time_range and f"time-{time_range}") or f"lines-{lines}")
    path = os.path.join(outdir, name + ".txt")
    crit = {}
    if time_range:
        a_s, _, b_s = str(time_range).partition("..")
        ta, tb = C.parse_ts_text(a_s, e.year()), C.parse_ts_text(b_s, e.year()) if b_s else None
        la = seek_time_line(e, ta, ctl) if ta is not None else None
        if la is None:
            raise HbmxError("no lines at or after that time")
        lb = (seek_time_line(e, tb, ctl) or e.lines + 1) - 1 if tb is not None else e.lines
        lines, crit["time"] = f"{la}:{lb}", time_range
    if pattern:
        crit.update({"pattern": pattern, "regex": regex, "ignore_case": icase})
        if lines:
            crit["lines"] = lines
        if e.codec or not e.text:
            a, start, end = 1, 0, None
        else:
            a, b = e.line_range(lines)
            start = e.line_offset(a) or 0
            nxt = e.line_offset(b + 1)
            end = nxt if nxt is not None else e.size
        flags = (["-F"] if not regex else []) + (["-i"] if icase else [])
        whole = end is None or (start == 0 and end >= e.size)
        cmd = ["rg", "-a", "--no-filename", "--no-line-number", "--color", "never", *flags] + (["-z"] if e.codec else []) + \
              ["-e", pattern, e.path if whole else "-"]
        with open(path, "wb") as out:
            p = subprocess.Popen(cmd, stdin=None if whole else subprocess.PIPE, stdout=out, stderr=subprocess.PIPE)
            if not whole:
                threading.Thread(target=_feed, args=(p, e.path, start, end), daemon=True).start()
            while p.poll() is None:
                try:
                    ctl.tick("extracting", 0.0)
                except BaseException:
                    p.kill()
                    raise
                time.sleep(0.05)
    elif where:
        conds = compile_where(where)
        crit["where"] = [f"{f}{op}{v}" for f, op, v, _ in conds]
        with open(path, "wb") as out:
            for _, _, raw in LineScan(e, lines, exact=True, ctl=ctl):
                if match_where(parse_record(e, raw), conds):
                    out.write(raw if raw.endswith(b"\n") else raw + b"\n")
    elif lines:
        e.require_lines()
        a, b = e.line_range(lines)
        crit["lines"] = f"{a}:{b}"
        start = e.line_offset(a) or 0
        nxt = e.line_offset(b + 1)
        end = nxt if nxt is not None else e.size
        with open(e.path, "rb") as src, open(path, "wb") as out:
            src.seek(start)
            left = end - start
            while left > 0:
                buf = src.read(min(left, 8 << 20))
                if not buf:
                    break
                out.write(buf)
                left -= len(buf)
                ctl.tick("copying", 1 - left / max(1, end - start))
    else:
        raise HbmxError("extract needs pattern, lines, time or where")
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        nlines = sum(buf.count(b"\n") for buf in iter(lambda: f.read(8 << 20), b""))
    rec = {"name": name, "path": path, "criteria": crit, "lines": nlines, "bytes": size, "created": C.now_iso()}
    ex = [x for x in e.meta.get("extracts") or [] if x.get("name") != name] + [rec]
    e.meta["extracts"] = ex[-50:]
    e.save()
    return (f"# extract {name} · {num(nlines)} lines · {human(size)} written\n  path: {path}\n  criteria: "
            f"{json.dumps(crit, ensure_ascii=False)}\n# next: open path={path} (it gets its own map) or view it there")


def inflate(e: Entry, ctl: Ctl = NOCTL) -> Tuple[str, Optional[Entry]]:
    if not e.codec:
        raise HbmxError("not a compressed file")
    e.require_file()
    st = e.meta.get("structure") or {}
    est = st.get("uncompressed") or e.size * 6
    free = shutil.disk_usage(os.path.dirname(e.ddir) if os.path.isdir(e.ddir) else C.HOME).free
    if est * 1.1 > free:
        raise HbmxError(f"not enough disk: about {human(est)} needed, {human(free)} free")
    os.makedirs(e.ddir, exist_ok=True)
    name = (st.get("original_name") or re.sub(r"\.(gz|bz2|xz|zst|lz4|tgz)$", "", e.name)) or "inflated"
    path = os.path.join(e.ddir, "inflated-" + _safe_name(name))
    with open(path + ".tmp", "wb") as out:
        p = subprocess.Popen(C.DECOMP[e.codec] + [e.path], stdout=out, stderr=subprocess.PIPE)
        while p.poll() is None:
            try:
                ctl.tick(f"inflating {human(os.path.getsize(path + '.tmp'))}", min(0.99, os.path.getsize(path + ".tmp") / est))
            except BaseException:
                p.kill()
                raise
            time.sleep(0.1)
        if p.returncode != 0:
            raise HbmxError("decompressor failed: " + p.stderr.read().decode("utf-8", "replace")[:300])
    os.replace(path + ".tmp", path)
    inner, _ = C.probe(path, ctl=ctl)
    e.meta["inflated"] = {"path": path, "sha256": inner.sha, "size": inner.size}
    e.save()
    inner.meta["inflated_from"] = {"sha256": e.sha, "path": e.path}
    inner.save()
    return (f"# inflated {e.name} → {path} ({human(inner.size)}), mapped as @{inner.sha[:12]}\n" + C.render_brief(inner), inner)


# ---------------------------------------------------------------- functions (helper scripts kept in memory)
NAME_RX = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")


def functions(e: Entry) -> str:
    fns = e.meta.get("functions") or []
    out = [f"# functions of {e.name} · {e.fdir} · {len(fns)}"]
    for f in fns:
        runs = f" · runs {f.get('runs', 0)}" + (f", last {str(f.get('last_run'))[:16]} rc={f.get('last_rc')}" if f.get("last_run") else "")
        out.append(f"  {f['name']:<14} [{f.get('by')}/{f.get('lang')}] {clip(f.get('description'), 80)}\n"
                   f"  {'':<14} usage: {f.get('usage')}{runs}")
    return "\n".join(out)


def function_source(e: Entry, name: str) -> str:
    f = _fn(e, name)
    p = os.path.join(e.edir, f["path"])
    with open(p, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    body = "\n".join(lines[:150]) + (f"\n… +{len(lines) - 150} lines" if len(lines) > 150 else "")
    return f"# function {name} [{f.get('by')}] · {p}\n{body}"


def _fn(e: Entry, name: str) -> dict:
    f = next((f for f in e.meta.get("functions") or [] if f["name"] == name), None)
    if not f:
        raise HbmxError(f"no function {name!r}: " + ", ".join(x["name"] for x in e.meta.get("functions") or []))
    return f


def write_function(e: Entry, name: str, lang: str, code: str, description: str, usage: str, by: str = "llm") -> str:
    name = str(name or "").strip().lower()
    if not NAME_RX.match(name):
        raise HbmxError("function name: lowercase letters, digits, _ or -, 2-41 chars, starting with a letter")
    lang = str(lang or "bash").lower()
    if lang not in ("bash", "sh", "python", "python3", "awk"):
        raise HbmxError("lang must be bash or python")
    code = str(code or "").strip("\n")
    if not code.strip():
        raise HbmxError("empty code")
    shebang = {"bash": "#!/usr/bin/env bash", "sh": "#!/bin/sh", "python": f"#!/usr/bin/env {os.path.basename(C.PYTHON)}",
               "python3": f"#!/usr/bin/env {os.path.basename(C.PYTHON)}", "awk": "#!/usr/bin/awk -f"}[lang]
    if code.startswith("#!"):
        shebang, code = code.split("\n", 1)[0], code.split("\n", 1)[1] if "\n" in code else ""
    hdr = (f"{shebang}\n# hbmx function «{name}» for {e.fmt} file {e.sha[:12]} ({e.name})\n# what : {clip(description, 200)}\n"
           f"# usage: {clip(usage, 200)}\n# by   : {by} · {C.now_iso()}\n"
           "# env  : HBMX_FILE (the file) HBMX_SHA HBMX_DATA (its data dir) HBMX_META (its map) HBMX_CORE HBMX_PYTHON\n")
    os.makedirs(e.fdir, exist_ok=True)
    p = os.path.join(e.fdir, name)
    existed = os.path.exists(p)
    if existed:
        shutil.copyfile(p, p + ".prev")
    C.write_atomic(p, hdr + code + "\n")
    os.chmod(p, 0o755)
    fns = [f for f in e.meta.get("functions") or [] if f["name"] != name]
    fns.append({"name": name, "path": os.path.relpath(p, e.edir), "lang": lang, "by": by,
                "description": clip(description, 200), "usage": clip(usage, 200), "created": C.now_iso(), "runs": 0})
    e.meta["functions"] = sorted(fns, key=lambda f: (f.get("by") != "hbmx", f["name"]))
    e.save()
    return f"# function {name} {'replaced (old copy: ' + name + '.prev)' if existed else 'saved'} · {p} · {len(code.splitlines())} lines"


def run_function(e: Entry, name: str, args: Optional[List[str]] = None, timeout: float = 120, max_bytes: int = 1 << 20,
                 ctl: Ctl = NOCTL) -> Tuple[int, str, float]:
    f = _fn(e, name)
    p = os.path.join(e.edir, f["path"])
    if not os.path.isfile(p):
        raise HbmxError(f"function file missing: {p}")
    env = dict(os.environ, HBMX_FILE=e.path or "", HBMX_SHA=e.sha, HBMX_DATA=e.ddir, HBMX_META=e.meta_file,
               HBMX_CORE=C.CORE, HBMX_PYTHON=C.PYTHON)
    os.makedirs(e.ddir, exist_ok=True)
    args = [str(a) for a in (args or [])]
    t0 = time.time()
    proc = subprocess.Popen([p, *args], cwd=e.ddir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    buf = bytearray()
    truncated = [False]

    def pump():
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            if len(buf) < max_bytes:
                buf.extend(chunk[: max_bytes - len(buf)])
            else:
                truncated[0] = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    try:
        while proc.poll() is None:
            ctl.tick(f"running {name} {time.time() - t0:.0f}s", 0.0)
            if time.time() - t0 > timeout:
                proc.kill()
                buf.extend(f"\n[hbmx: killed after {timeout:.0f}s]".encode())
                break
            time.sleep(0.05)
    except BaseException:
        proc.kill()
        raise
    th.join(2)
    rc = proc.wait()
    secs = round(time.time() - t0, 2)
    out = buf.decode("utf-8", "replace")
    if truncated[0]:
        out += f"\n[hbmx: output stopped at {human(max_bytes)}]"
    f.update({"runs": int(f.get("runs") or 0) + 1, "last_run": C.now_iso(), "last_rc": rc, "last_secs": secs})
    e.save()
    return rc, out, secs


def clip_output(text: str, max_chars: int, max_line: int = 400) -> str:
    """Bound a tool output for the LLM: long lines clipped, then head + tail kept with the cut stated."""
    lines = [l if len(l) <= max_line else l[:max_line] + f" …[+{len(l) - max_line} chars]" for l in text.splitlines()]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.7)]
    tail = text[-int(max_chars * 0.25):]
    cut = text.count("\n") - head.count("\n") - tail.count("\n")
    return f"{head}\n… [{cut} lines / {len(text) - len(head) - len(tail)} chars cut] …\n{tail}"


# ---------------------------------------------------------------- notes (the agent and the user grow the map)
def note(e: Entry, kind: str, by: str = "llm", label: str = "", line=None, byte=None, time_=None, title: str = "",
         detail: str = "", refs=None, severity: str = "", text: str = "") -> str:
    kind = (kind or "").lower()
    if kind == "landmark":
        if not label:
            raise HbmxError("a landmark needs a label")
        lm = {"label": clip(label, 120), "by": by, "at": C.now_iso()}
        if time_ is not None:
            t = C.parse_ts_text(time_, e.year())
            line = seek_time_line(e, t) if t is not None else None
        if line is not None:
            lm["line"] = int(line)
            if e.random_access:
                lm["byte"] = e.line_offset(int(line))
                rows = read_lines(e, int(line), 1)
                if rows:
                    lm["text"] = show_line(rows[0][1], 160)
        elif byte is not None:
            lm["byte"] = int(byte)
            if e.random_access:
                lm["line"] = e.byte_line(int(byte))
        else:
            raise HbmxError("a landmark needs line, byte or time")
        lms = e.meta.setdefault("landmarks", [])
        lm["id"] = f"L{max([int(l['id'][1:]) for l in lms if str(l.get('id', '')).startswith('L')] + [0]) + 1}"
        lms.append(lm)
        e.save()
        return f"# landmark {lm['id']} saved: {lm['label']} · line {lm.get('line')} @byte {lm.get('byte')}"
    if kind == "finding":
        import hbmx_know
        tags = [severity] if severity else []
        fid, added = hbmx_know.add_finding(e, title, detail, refs, tags=tags, by=by)
        n = len(hbmx_know.load(e)["findings"])
        return (f"# finding {fid} saved ({n} on this file): {clip(title, 120)}" if added else
                f"# finding {fid} already known: confirmed again ({clip(title, 120)})")
    if kind == "howto":
        t = clip(text or detail or title, 300)
        if not t:
            raise HbmxError("a howto needs text")
        hw = e.meta.setdefault("howto", [])
        if t not in hw:
            hw.append(t)
        e.save()
        return f"# howto saved: {t}"
    raise HbmxError("note kind is landmark, finding or howto")


# ---------------------------------------------------------------- CLI (also used by the template functions)
def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="hbmx_core.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("view")
    s.add_argument("file")
    s.add_argument("--line", type=int)
    s.add_argument("--count", type=int, default=40)
    s.add_argument("--byte", type=int)
    s.add_argument("--length", type=int, default=256)
    s.add_argument("--region")
    s.add_argument("--time", dest="time_range", help="FROM..TO or FROM")
    s.add_argument("--width", type=int, default=240)
    s = sub.add_parser("record")
    s.add_argument("file")
    s.add_argument("--line", type=int, required=True)
    s = sub.add_parser("seek")
    s.add_argument("file")
    s.add_argument("--line", type=int)
    s.add_argument("--byte", type=int)
    s.add_argument("--time")
    s = sub.add_parser("search")
    s.add_argument("file")
    s.add_argument("pattern")
    s.add_argument("--fixed", action="store_true")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.add_argument("--max", type=int, default=30)
    s.add_argument("--lines")
    s.add_argument("--count", action="store_true", default=None)
    s.add_argument("--after-line", type=int)
    s = sub.add_parser("filter")
    s.add_argument("file")
    s.add_argument("--where", action="append", required=True)
    s.add_argument("--fields")
    s.add_argument("--max", type=int, default=30)
    s.add_argument("--lines")
    s.add_argument("--count", action="store_true")
    s = sub.add_parser("stats")
    s.add_argument("file")
    s.add_argument("--kind", required=True)
    s.add_argument("--field")
    s.add_argument("--regex")
    s.add_argument("--exact", action="store_true", default=None)
    s.add_argument("--lines")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--where", action="append")
    s.add_argument("--baseline")
    s = sub.add_parser("extract")
    s.add_argument("file")
    s.add_argument("--name")
    s.add_argument("--pattern")
    s.add_argument("--fixed", action="store_true")
    s.add_argument("-i", "--ignore-case", action="store_true")
    s.add_argument("--lines")
    s.add_argument("--time", dest="time_range")
    s.add_argument("--where", action="append")
    sub.add_parser("inflate").add_argument("file")
    sub.add_parser("functions").add_argument("file")
    s = sub.add_parser("run")
    s.add_argument("file")
    s.add_argument("function")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s = sub.add_parser("note")
    s.add_argument("file")
    s.add_argument("--kind", required=True, choices=["landmark", "finding", "howto"])
    s.add_argument("--label", default="")
    s.add_argument("--line", type=int)
    s.add_argument("--byte", type=int)
    s.add_argument("--time")
    s.add_argument("--title", default="")
    s.add_argument("--detail", default="")
    s.add_argument("--text", default="")
    a = p.parse_args(argv)
    try:
        e = C.resolve(a.file)
        if a.cmd == "view":
            print(view(e, a.line, a.count, a.byte, a.length, a.region, a.time_range, a.width))
        elif a.cmd == "record":
            print(record(e, a.line))
        elif a.cmd == "seek":
            print(seek(e, a.line, a.byte, a.time))
        elif a.cmd == "search":
            print(search(e, a.pattern, not a.fixed, a.ignore_case, a.max, a.lines, a.count, a.after_line))
        elif a.cmd == "filter":
            print(filter_records(e, a.where, a.fields, a.max, a.lines, a.count))
        elif a.cmd == "stats":
            print(stats(e, a.kind, a.field, a.regex, a.exact, a.lines, a.top, a.where, a.baseline))
        elif a.cmd == "extract":
            print(extract(e, a.name, a.pattern, not a.fixed, a.ignore_case, a.lines, a.time_range, a.where))
        elif a.cmd == "inflate":
            print(inflate(e)[0])
        elif a.cmd == "functions":
            print(functions(e))
        elif a.cmd == "run":
            rc, out, secs = run_function(e, a.function, a.args)
            print(out, end="" if out.endswith("\n") else "\n")
            return rc
        elif a.cmd == "note":
            print(note(e, a.kind, by="user", label=a.label, line=a.line, byte=a.byte, time_=a.time, title=a.title,
                       detail=a.detail, text=a.text))
    except (HbmxError, C.Cancelled) as ex:
        print(f"hbmx: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
