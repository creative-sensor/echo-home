#!/usr/bin/env python
"""
gml8_core: the engine of gml8, the operator of this machine's llama.cpp LLM services. Frontend gml8.lua, backend
gml8.py, network gml8_iface.py, discovery bus gml8bus.lua (all in ~/echo-home/.vim/mimd/gmlama). Usable from the shell:

    python gml8_core.py status | entries | entry <ref> | merge [--dry] | launch <ref> [--params ".."] [--port N] |
                        stop <ref|port> | logs <ref|port> | diagnose <ref|port> | services | discover [--kind chat]
                        [--vision] [--min-ctx N] [--name X] [--port-only] | gguf <file> | hw | history [ref]
    <ref>: a line number (11, #11), a named entry (@MBG4_MMDI_disp or MBG4_MMDI_disp), or part of a model file name.

INVENTORY  ~/echo-home/workstation/lmstudio/gmlama/.properd/gml.<hostname>.yson — one entry per line:
    <model_filename>: ["<ACCEL>/<profile_name>", "<llama-server params>", "<llama.cpp version>", "<gpu device>"]
  * RESERVED: the top of the file, up to the first "---- <SECTION> ----" line. Line N is the slot of line N of
    artefact-series.gml (the series of model files). merge() gives each series line without an entry the placeholder
    "<model_filename>: []" (a new series line: a new reserved line, inserted before the first section). Reserved
    entries are open: gml8 adapts them to this machine when it launches them (a free --port when theirs is taken or
    missing) and writes what it changed back to their line.
  * NAMED: the entries in the sections after (---- NAMED ---- … ---- NAMED.end ----, ---- TEST ---- …), addressed by
    their profile name (@MBG4_MMDI_disp): their line moves when the series grows. Launched exactly as written (never
    adapted). A 5th element, a map, sets up complex launches:
        {env: {K: V}, script: <name|path>, pre: <shell>, post: <shell>, port: N, wait: <secs>}
    script: an executable (.function/named/<name>, or a path) started instead of llama-server, with GML8_BIN,
    GML8_MODEL, GML8_PARAMS, GML8_DEV, GML8_PORT, GML8_LOG, GML8_KEY, GML8_NAME, GML8_CWD in its environment; it
    must keep running while the service runs (exec llama-server at its end). A named entry with only a profile name
    (["MEGALOADER"]) runs .function/named/MEGALOADER when that file exists. pre runs before the launch (its failure
    stops it), post once the service answers.
  ACCEL: GPU → $LOCAL_BIN/gmlama-vulkan, CPU or none → $LOCAL_BIN/gmlama, CUDA → gmlama-cuda, X → gmlama-x; the version
  picks a subfolder (b11480). Params and device go through envsubst (an unset $VAR is empty, as in gml8w); GML_DEV is
  the device of GPU entries that name none. Models live in $GMLBASE (~/.lmstudio/models).

MEMORY  ~/echo-home/memory/gml8/<hostname>/ (UTC everywhere): events.jsonl (launch, up, stop, crash, commit, merge …),
  entries.json (per entry: launches, load times, speeds, failures, notes), run/runs.json (every run: pid, port, log,
  state), run/services.json (the discovery registry other agents read), logs/<key>-<port>-<stamp>.log (each server's
  output), gguf.json (GGUF metadata cache), inventory.bak/ (the inventory before each write by gml8).
Services run detached (their own session): they outlive gml8, Neovim and the terminal; gml8 adopts them at start.
"""
import json
import os
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

HOME = os.path.expanduser("~")
GMLAMA = os.path.join(HOME, "echo-home", "workstation", "lmstudio", "gmlama")
PROPERD = os.path.join(GMLAMA, ".properd")
NAMED_DIR = os.path.join(GMLAMA, ".function", "named")
DEFAULT_MEMORY = os.path.join(HOME, "echo-home", "memory", "gml8")
WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""

SEP_RE = re.compile(r"^\s*#?\s*-{3,}\s*([A-Za-z0-9_.\- ]*?)\s*-{3,}\s*$")
ICON = {"up": "✅", "loading": "⏳", "launching": "🚀", "stalled": "⌛", "crashed": "💥", "failed": "❌",
        "stopped": "⏹", "stopping": "⏏", "exited": "⏹", "down": "🟠", "foreign": "🔀"}
ACTIVE = ("launching", "loading", "up", "stalled", "stopping")


# ---------------------------------------------------------------- small helpers
def hostname() -> str:
    return os.environ.get("GML8_HOSTNAME") or socket.gethostname()


def utc_now(t: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def stamp(t: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if t is None else t))


def parse_utc(s) -> Optional[float]:
    try:
        return float(__import__("calendar").timegm(time.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def when(s) -> str:
    """A stored UTC time shown with its year: 2026-11-27 10:02Z."""
    t = parse_utc(s) if isinstance(s, str) else s
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(t)) if t else str(s or "")


def ago(t: Optional[float]) -> str:
    if not t:
        return ""
    s = max(0, int(time.time() - t))
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h{(s % 3600) // 60:02d}" if s < 172800 else f"{s // 86400}d"


def clip(s, n: int = 300) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def gib(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    return f"{n / 2 ** 30:.2f} GiB" if n >= 2 ** 30 * 0.95 else f"{n / 2 ** 20:.0f} MiB"


def tilde(p: str) -> str:
    return "~" + p[len(HOME):] if p and p.startswith(HOME) else p


def default_inventory() -> str:
    return os.path.join(PROPERD, f"gml.{hostname()}.yson")


def default_series() -> str:
    return os.path.join(PROPERD, "artefact-series.gml")


def gmlbase() -> str:
    return os.path.expanduser(os.environ.get("GMLBASE") or "~/.lmstudio/models")


def local_bin() -> str:
    return os.path.expanduser(os.environ.get("LOCAL_BIN") or "~/.local/bin")


_VAR = re.compile(r"\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def envsubst(s: str, env: Optional[dict] = None, missing: Optional[list] = None) -> str:
    """As envsubst(1): $VAR / ${VAR} from the environment, an unset one is empty (and noted in missing)."""
    env = os.environ if env is None else env

    def sub(m):
        name = m.group(2) or m.group(3)
        if name in env:
            return env[name]
        if missing is not None and name not in missing:
            missing.append(name)
        return ""
    return _VAR.sub(sub, s or "")


def winfix(s: str) -> str:
    """gml8w's conversion: C:\\x -> /c/x, backslashes -> slashes."""
    s = re.sub(r"([A-Z]):\\", lambda m: "/" + m.group(1).lower() + "/", s or "")
    return s.replace("\\", "/")


def native_paths(s: str) -> str:
    """Paths in params as this machine's llama-server reads them. On Windows gml8 (a native Python) starts
    llama-server.exe itself, and nothing converts MSYS paths in its arguments there: C:\\x and /c/x -> C:/x
    (backslashes too: the params are split as a shell would, which eats them). Elsewhere: gml8w's winfix."""
    if not WINDOWS:
        return winfix(s)
    s = (s or "").replace("\\", "/")
    return re.sub(r"(^|[\s=\"'])/([a-zA-Z])(?=/)", lambda m: m.group(1) + m.group(2).upper() + ":", s)


def expand_params(s: str, missing: Optional[list] = None) -> str:
    """An entry's params (or device) ready to launch: $VARs from the environment, paths native (native_paths)."""
    return native_paths(envsubst(s or "", missing=missing))


def atomic_write(path: str, text: str):
    """Every file gml8 writes goes through here (or event()): utf-8, unix LF line ends — on Windows too."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- llama-server params
ALIAS = {
    "-c": "ctx", "--ctx-size": "ctx", "-b": "batch", "--batch-size": "batch", "-ub": "ubatch", "--ubatch-size": "ubatch",
    "-np": "parallel", "--parallel": "parallel", "-ctk": "ctk", "--cache-type-k": "ctk", "-ctv": "ctv",
    "--cache-type-v": "ctv", "-kvo": "kvo", "--kv-offload": "kvo", "-nkvo": "nkvo", "--no-kv-offload": "nkvo",
    "-fa": "fa", "--flash-attn": "fa", "--swa-full": "swafull", "-ngl": "ngl", "--n-gpu-layers": "ngl",
    "--gpu-layers": "ngl", "-dev": "dev", "--device": "dev", "-sm": "sm", "--split-mode": "sm", "-ts": "ts",
    "--tensor-split": "ts", "-mg": "mg", "--main-gpu": "mg", "-fit": "fit", "--fit": "fit", "--no-mmap": "nommap",
    "--mmap": "mmap", "--mlock": "mlock", "-cmoe": "cpumoe", "--cpu-moe": "cpumoe", "-ncmoe": "ncpumoe",
    "--n-cpu-moe": "ncpumoe", "-ot": "ot", "--override-tensor": "ot", "-t": "threads", "--threads": "threads",
    "-tb": "threadsb", "--threads-batch": "threadsb", "-mm": "mmproj", "--mmproj": "mmproj",
    "--no-mmproj-offload": "nommproj", "--port": "port", "--host": "host", "-a": "alias", "--alias": "alias",
    "--api-key": "apikey", "--timeout": "timeout", "-to": "timeout", "--slot-save-path": "slotsave",
    "--embedding": "embed", "--embeddings": "embed", "--pooling": "pooling", "--rerank": "rerank",
    "--reranking": "rerank", "--jinja": "jinja", "--no-jinja": "nojinja", "-m": "model", "--model": "model",
    "-md": "draft", "--model-draft": "draft", "--temp": "temp", "--top-k": "topk", "--top-p": "topp",
    "--min-p": "minp", "--repeat-penalty": "rpen", "--repeat-last-n": "rlast", "-n": "npredict",
    "--n-predict": "npredict", "--rope-scaling": "rope", "-s": "seed", "--seed": "seed", "--metrics": "metrics",
    "--slots": "slots", "--no-slots": "noslots", "-lm": "lm", "--load-mode": "lm", "--reasoning-budget": "rbudget",
    "--chat-template": "tmpl", "--chat-template-file": "tmplfile", "-st": "st", "--no-warmup": "nowarmup",
}
BOOL = {"kvo", "nkvo", "swafull", "nommap", "mmap", "mlock", "cpumoe", "nommproj", "embed", "rerank", "jinja",
        "nojinja", "metrics", "slots", "noslots", "st", "nowarmup"}
SAY = {  # what a flag does, for MODEL IDX
    "ctx": "context size", "batch": "logical batch", "ubatch": "physical batch", "parallel": "slots",
    "ctk": "KV cache K type", "ctv": "KV cache V type", "kvo": "KV cache on the GPU", "nkvo": "KV cache in RAM",
    "fa": "flash attention", "swafull": "full-size SWA cache", "ngl": "layers on the GPU", "dev": "device",
    "sm": "split mode", "ts": "tensor split", "mg": "main GPU", "fit": "fit params to free memory",
    "nommap": "load into memory (no mmap)", "mmap": "mmap the model", "mlock": "lock in RAM", "cpumoe": "MoE experts on CPU",
    "ncpumoe": "MoE layers on CPU", "ot": "tensor overrides", "threads": "threads", "threadsb": "batch threads",
    "mmproj": "vision projector", "nommproj": "projector stays on CPU", "port": "port", "host": "bind address",
    "alias": "model alias", "apikey": "API key", "timeout": "timeout (s)", "slotsave": "slot save path",
    "embed": "embeddings only", "pooling": "pooling", "rerank": "reranking", "jinja": "jinja template",
    "temp": "temperature", "topk": "top-k", "topp": "top-p", "minp": "min-p", "rpen": "repeat penalty",
    "rlast": "repeat window", "npredict": "max tokens", "rope": "rope scaling", "seed": "seed",
    "draft": "draft model", "lm": "load mode", "rbudget": "reasoning budget", "st": "single turn",
}
GROUPS = [("context", ["ctx", "batch", "ubatch", "parallel"]),
          ("kv cache", ["ctk", "ctv", "kvo", "nkvo", "fa", "swafull"]),
          ("offload", ["ngl", "dev", "sm", "ts", "mg", "fit", "nommap", "mmap", "mlock", "lm", "cpumoe", "ncpumoe", "ot",
                       "threads", "threadsb"]),
          ("vision", ["mmproj", "nommproj"]),
          ("server", ["port", "host", "alias", "apikey", "timeout", "slotsave", "embed", "pooling", "rerank", "jinja",
                      "nojinja", "metrics", "slots", "noslots", "tmpl", "tmplfile", "rbudget"]),
          ("sampling", ["temp", "topk", "topp", "minp", "rpen", "rlast", "npredict", "seed", "rope"])]
SECRET = {"apikey"}


def _is_value(tok: str) -> bool:
    return not tok.startswith("-") or bool(re.match(r"^-\d", tok))


def split_params(params: str) -> List[str]:
    try:
        return shlex.split(params or "")
    except ValueError:
        return (params or "").split()


def parse_params(params) -> List[list]:
    """'-c 8192 -kvo --port 8090' -> [['-c','8192'], ['-kvo',None], ['--port','8090']] (order kept)."""
    toks = params if isinstance(params, list) else split_params(params)
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("-") and not re.match(r"^-\d", t):
            if "=" in t and t.startswith("--"):
                k, v = t.split("=", 1)
                out.append([k, v])
                i += 1
                continue
            name = ALIAS.get(t)
            if name not in BOOL and i + 1 < len(toks) and _is_value(toks[i + 1]):
                out.append([t, toks[i + 1]])
                i += 2
                continue
            out.append([t, None])
        else:
            out.append([None, t])  # a stray value
        i += 1
    return out


def quote(v: str) -> str:
    """Only what has blanks is quoted: params are split by words (gml8w), and $VARs stay as written."""
    return shlex.quote(v) if v == "" or re.search(r"\s", v) else v


def join_params(pairs: List[list]) -> str:
    out = []
    for f, v in pairs:
        if f:
            out.append(f)
        if v is not None:
            out.append(quote(v))
    return " ".join(out)


def pget(pairs: List[list], name: str):
    """The value of a flag by its name ('ctx', 'port' …): the last one wins. True for a boolean flag, None if absent."""
    val = None
    for f, v in pairs:
        if f and ALIAS.get(f, f) == name:
            val = True if v is None else v
    return val


def pset(pairs: List[list], flag: str, value=None) -> List[list]:
    """Set a flag (replacing every spelling of it) or, value False, remove it. flag: '-c', '--port', 'ctx' …"""
    name = ALIAS.get(flag, flag)
    canon = flag if flag.startswith("-") else next((k for k, v in ALIAS.items() if v == name and k.startswith("-")), "--" + name)
    out, done = [], False
    for f, v in pairs:
        if f and ALIAS.get(f, f) == name:
            if value is False or done:
                continue
            out.append([f, None if value is True or value is None else str(value)])
            done = True
        else:
            out.append([f, v])
    if not done and value is not False:
        out.append([canon, None if value is True or value is None else str(value)])
    return out


def ngl_fraction(ngl, n_layer: int) -> float:
    if ngl is None or ngl is True:
        return 1.0  # recent llama.cpp offloads everything it can by default
    s = str(ngl).strip().lower()
    if s in ("all", "auto", "max", "-1"):
        return 1.0
    try:
        n = int(float(s))
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, n / float(max(1, n_layer + 1))))


# ---------------------------------------------------------------- inventory
class Entry:
    """One line of the inventory."""

    def __init__(self, line: int, raw: str):
        self.line, self.raw = line, raw
        self.kind = "blank"  # entry | placeholder | blank | comment | sep | bad
        self.section = "RESERVED"
        self.model = self.profile = self.params = self.version = self.device = self.comment = ""
        self.extra: dict = {}
        self.slot = 0  # series line number (reserved)
        self.series = ""  # the series model of this slot
        self.key = f"#{line}"

    @property
    def reserved(self) -> bool:
        return self.section == "RESERVED"

    @property
    def launchable(self) -> bool:
        return self.kind in ("entry", "placeholder")

    @property
    def accel(self) -> str:
        return self.profile.split("/", 1)[0].upper() if "/" in self.profile else "CPU"

    @property
    def pname(self) -> str:
        return self.profile.split("/", 1)[1] if "/" in self.profile else self.profile

    @property
    def short(self) -> str:
        return os.path.splitext(os.path.basename(self.model))[0] if self.model else ""

    def label(self) -> str:
        p = self.pname
        return f"{self.key} {self.short}" + (f" ({self.profile})" if self.profile and not self.key.startswith("@") else "") \
            if self.reserved else f"{self.key} {self.short}"

    def values(self) -> dict:
        return {"model": self.model, "profile": self.profile, "params": self.params, "version": self.version,
                "device": self.device, "extra": dict(self.extra)}

    def brief(self) -> dict:
        return {"line": self.line, "key": self.key, "kind": self.kind, "section": self.section, "model": self.model,
                "profile": self.profile, "params": mask(self.params), "version": self.version, "device": self.device,
                "slot": self.slot, "series": self.series}


def mask(params: str) -> str:
    return re.sub(r"(--api-key(?:=|\s+))(\S+)", r"\1***", params or "")


def _scalar(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "on" if v else "off"
    s = str(v)
    return "" if s == "null" else s


def parse_entry_text(text: str):
    """'model: ["GPU/x", "params", "ver", "dev", {..}]  # note' -> (model, [values], comment) or None."""
    s = text.strip()
    if not s or s.startswith("#") or ":" not in s:
        return None
    comment = ""
    m = re.search(r"\]\s*(#.*)$", s)
    if m:
        comment = m.group(1).strip()
    try:
        import yaml
        data = yaml.safe_load(s)
        if isinstance(data, dict) and len(data) == 1:
            model, vals = next(iter(data.items()))
            model = str(model).rstrip(":").strip()
            if vals is None:
                vals = []
            if not isinstance(vals, list):
                vals = [vals]
            return model, vals, comment
    except Exception:
        pass
    m = re.match(r"^\s*(.+?)\s*:+\s*\[(.*)\]\s*(#.*)?$", s)  # the regex way, as gml8s
    if not m:
        return None
    vals = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2))
    return m.group(1).strip().strip("\"'"), vals, comment


def format_entry(model: str, profile="", params="", version="", device="", extra=None, comment="") -> str:
    vals = [profile or "", params or "", version or "", device or ""]
    if extra:
        vals.append(extra)
    else:
        while vals and not vals[-1]:
            vals.pop()
    if not vals:
        body = "[]"
    else:
        enc = [json.dumps(v, ensure_ascii=False) for v in vals]
        body = "[" + enc[0] + ("" if len(enc) == 1 else " , " + ", ".join(enc[1:])) + "]"
    return f"{model}: {body}" + (f" {comment}" if comment else "")


def read_series(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [l.strip() for l in f.read().split("\n")]
    except OSError:
        return []


class Inventory:
    def __init__(self, path: str, series_path: str):
        self.path, self.series_path = path, series_path
        self.sig = None
        self.items: List[Entry] = []
        self.series: List[str] = []
        self.reserved_end = 0  # the number of reserved lines
        self.lock = threading.RLock()

    def _signature(self):
        sig = []
        for p in (self.path, self.series_path):
            try:
                st = os.stat(p)
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def load(self, force: bool = False) -> List[Entry]:
        with self.lock:
            sig = self._signature()
            if not force and sig == self.sig:
                return self.items
            self.sig = sig
            try:
                with open(self.path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = ""
            self.series = read_series(self.series_path)
            self.items = parse_inventory(text, self.series)
            self.reserved_end = next((e.line - 1 for e in self.items if e.kind == "sep"), len(self.items))
            return self.items

    def entries(self) -> List[Entry]:
        return [e for e in self.load() if e.launchable]

    def get(self, line: int) -> Optional[Entry]:
        items = self.load()
        return items[line - 1] if 0 < line <= len(items) else None

    def by_key(self, key: str) -> Optional[Entry]:
        return next((e for e in self.load() if e.key == key), None)

    def find(self, ref) -> Optional[Entry]:
        """11 / '#11' / '@NAME' / 'NAME' (a profile name) / part of a model file name."""
        if ref is None:
            return None
        s = str(ref).strip()
        if re.fullmatch(r"#?\d+", s):
            e = self.get(int(s.lstrip("#")))
            return e if e and e.launchable else None
        items = self.entries()
        key = s if s.startswith("@") else "@" + s
        for e in items:
            if e.key.lower() == key.lower():
                return e
        low = s.lstrip("@").lower()
        for e in items:
            if e.pname and e.pname.lower() == low:
                return e
        for e in items:
            if low and low in e.model.lower():
                return e
        return None

    def lines(self) -> List[str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().split("\n")
        except OSError:
            return []

    def write_line(self, line: int, new_text: str, expect: Optional[str] = None, mem=None, why: str = "") -> str:
        """Replace line N of the file (the file as it is on disk: expect, when given, must still be its text)."""
        with self.lock:
            lines = self.lines()
            if not 0 < line <= len(lines):
                raise ValueError(f"line {line} is not in {self.path}")
            if expect is not None and lines[line - 1].rstrip() != expect.rstrip():
                raise ValueError(f"line {line} changed on disk (now: {clip(lines[line - 1], 120)}) — not written")
            if lines[line - 1] == new_text:
                return "unchanged"
            if mem:
                mem.backup(self.path)
            lines[line - 1] = new_text
            atomic_write(self.path, "\n".join(lines))
            self.load(force=True)
            if mem:
                mem.event("commit", line=line, key=self.get(line).key if self.get(line) else f"#{line}",
                          text=clip(mask(new_text), 300), why=why)
            return "written"


def parse_inventory(text: str, series: List[str]) -> List[Entry]:
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw = raw[:-1]
    items, section, seen = [], "RESERVED", {}
    for i, line in enumerate(raw, 1):
        e = Entry(i, line)
        m = SEP_RE.match(line)
        if m:
            e.kind = "sep"
            name = (m.group(1) or "").strip()
            e.model = name
            if name.upper().endswith(".END"):
                section = "OUTSIDE"
            else:
                section = name.upper() or "NAMED"
            e.section = section
            items.append(e)
            continue
        e.section = section
        if e.reserved:
            e.slot = i
            e.series = series[i - 1] if i <= len(series) and not series[i - 1].startswith("#") else ""
        s = line.strip()
        if not s:
            e.kind = "blank"
        elif s.startswith("#"):
            e.kind = "comment"
        else:
            parsed = parse_entry_text(line)
            if not parsed:
                e.kind = "bad"
            else:
                model, vals, comment = parsed
                e.model, e.comment = model, comment
                e.kind = "placeholder" if not vals else "entry"
                e.profile = _scalar(vals[0]) if len(vals) > 0 else ""
                e.params = _scalar(vals[1]) if len(vals) > 1 else ""
                e.version = _scalar(vals[2]) if len(vals) > 2 else ""
                e.device = _scalar(vals[3]) if len(vals) > 3 else ""
                e.extra = vals[4] if len(vals) > 4 and isinstance(vals[4], dict) else {}
                if not e.reserved:
                    base = e.pname or e.short
                    key = "@" + re.sub(r"[^A-Za-z0-9_.\-]+", "-", base)
                    if key in seen:
                        key = f"{key}~{i}"
                    seen[key] = i
                    e.key = key
        items.append(e)
    return items


def merge_series(inv_path: str, series_path: str, mem=None, dry: bool = False) -> dict:
    """Make the reserved lines follow the series: a series line N without an entry (its reserved line blank, or past
    the reserved lines) gets '<model>: []'. New reserved lines go before the first section; a missing NAMED section
    is added after the reserved lines. Lines that hold something (an entry, a comment) are never touched."""
    series = read_series(series_path)
    try:
        with open(inv_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    sep = next((i for i, l in enumerate(lines) if SEP_RE.match(l)), len(lines))
    reserved, rest = lines[:sep], lines[sep:]
    filled, added, mismatch = [], [], []
    last = max((i for i, s in enumerate(series, 1) if s and not s.startswith("#")), default=0)
    for i in range(1, last + 1):
        name = series[i - 1]
        if not name or name.startswith("#"):
            if i > len(reserved):
                reserved.append("")
            continue
        if i <= len(reserved):
            cur = reserved[i - 1]
            if not cur.strip():
                reserved[i - 1] = f"{name}: []"
                filled.append(i)
            else:
                p = parse_entry_text(cur)
                if p and p[0] != name:
                    mismatch.append(i)
        else:
            while len(reserved) < i - 1:
                reserved.append("")
            reserved.append(f"{name}: []")
            added.append(i)
    named_added = False
    if not any(SEP_RE.match(l) and (SEP_RE.match(l).group(1) or "").strip().upper() == "NAMED" for l in rest):
        rest = rest + ["---- NAMED ----", "---- NAMED.end ----"]
        named_added = True
    changed = bool(filled or added or named_added)
    res = {"filled": filled, "added": added, "named_added": named_added, "mismatch": mismatch, "changed": changed,
           "series": last, "path": inv_path}
    if changed and not dry:
        if mem:
            mem.backup(inv_path)
        atomic_write(inv_path, "\n".join(reserved + rest) + ("\n" if trailing or not text else ""))
        if mem:
            mem.event("merge", filled=filled, added=added, named_added=named_added, series=series_path)
    return res


def merge_text(res: dict) -> str:
    if not res.get("changed"):
        return f"series merged: nothing new ({res.get('series')} series lines)"
    parts = []
    if res["added"]:
        parts.append(f"new reserved line{'s' if len(res['added']) > 1 else ''} {', '.join('#%d' % i for i in res['added'])}")
    if res["filled"]:
        f = res["filled"]
        parts.append(f"{len(f)} empty slot{'s' if len(f) > 1 else ''} filled ({', '.join('#%d' % i for i in f[:8])}{' …' if len(f) > 8 else ''})")
    if res["named_added"]:
        parts.append("the ---- NAMED ---- section added")
    return "series merged: " + " · ".join(parts) + " (placeholders '<model>: []')"


# ---------------------------------------------------------------- model files and GGUF
def model_path(model: str) -> str:
    m = os.path.expanduser(envsubst(model))
    return m if os.path.isabs(m) else os.path.join(gmlbase(), m)


def model_parts(path: str) -> List[str]:
    m = re.match(r"^(.*-)(\d{5})-of-(\d{5})\.gguf$", path)
    if not m:
        return [path]
    n = int(m.group(3))
    return [f"{m.group(1)}{i:05d}-of-{n:05d}.gguf" for i in range(1, n + 1)]


def file_facts(path: str) -> dict:
    parts = model_parts(path)
    size, have = 0, 0
    mtime = None
    for p in parts:
        try:
            st = os.stat(p)
            size += st.st_size
            have += 1
            mtime = st.st_mtime if mtime is None else mtime
        except OSError:
            pass
    return {"path": path, "exists": have == len(parts), "parts": len(parts), "have": have, "size": size, "mtime": mtime}


def mmproj_near(path: str) -> List[Tuple[str, int]]:
    d = os.path.dirname(path)
    out = []
    try:
        for n in sorted(os.listdir(d)):
            if n.lower().startswith("mmproj") and n.endswith(".gguf"):
                try:
                    out.append((os.path.join(d, n), os.path.getsize(os.path.join(d, n))))
                except OSError:
                    pass
    except OSError:
        pass
    return out


_GT = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2), 4: ("I", 4), 5: ("i", 4), 6: ("f", 4), 7: ("?", 1),
       10: ("Q", 8), 11: ("q", 8), 12: ("d", 8)}


def read_gguf(path: str, max_array: int = 512) -> dict:
    """The metadata of a GGUF file (tensor infos skipped). Arrays: {'n': count, 'v': values when ≤ max_array}."""
    meta = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        version = struct.unpack("<I", f.read(4))[0]
        wide = version >= 2
        rd_n = (lambda: struct.unpack("<Q", f.read(8))[0]) if wide else (lambda: struct.unpack("<I", f.read(4))[0])
        n_tensors, n_kv = rd_n(), rd_n()
        meta["gguf.version"], meta["gguf.tensors"] = version, n_tensors

        def rd_str():
            n = rd_n()
            return f.read(n).decode("utf-8", "replace")

        def skip_str():
            n = rd_n()
            f.seek(n, 1)
            return n

        def rd_val(t, keep=True):
            if t in _GT:
                fmt, size = _GT[t]
                return struct.unpack("<" + fmt, f.read(size))[0]
            if t == 8:
                if keep:
                    return rd_str()
                skip_str()
                return None
            if t == 9:
                et = struct.unpack("<I", f.read(4))[0]
                n = rd_n()
                if et in _GT and n > max_array:
                    f.seek(n * _GT[et][1], 1)
                    return {"n": n, "type": et}
                if et == 8 and n > max_array:
                    for _ in range(n):
                        skip_str()
                    return {"n": n, "type": et}
                return {"n": n, "type": et, "v": [rd_val(et) for _ in range(n)]}
            raise ValueError(f"unknown GGUF value type {t}")

        for _ in range(n_kv):
            key = rd_str()
            t = struct.unpack("<I", f.read(4))[0]
            big = key == "tokenizer.chat_template" or key.startswith("tokenizer.ggml.merges")
            val = rd_val(t, keep=not big)
            if key == "tokenizer.chat_template":
                val = True
            meta[key] = val
    return meta


FTYPE = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
         12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS",
         20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
         28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4_MOE"}


def _arr(v):
    return v.get("v") if isinstance(v, dict) else None


def model_facts(meta: dict) -> dict:
    arch = str(meta.get("general.architecture") or "?")

    def g(k, default=None):
        return meta.get(f"{arch}.{k}", default)
    n_layer = int(g("block_count", 0) or 0)
    n_head = g("attention.head_count")
    n_head = (_arr(n_head) or [0])[0] if isinstance(n_head, dict) else n_head
    hkv = g("attention.head_count_kv")
    n_embd = int(g("embedding_length", 0) or 0)
    head_k = g("attention.key_length") or (n_embd // n_head if n_head else 0)
    head_v = g("attention.value_length") or head_k
    vocab = meta.get("tokenizer.ggml.tokens")
    vocab = vocab.get("n") if isinstance(vocab, dict) else g("vocab_size")
    swa = g("attention.sliding_window")
    pattern = _arr(g("attention.sliding_window_pattern"))
    return {
        "arch": arch, "name": meta.get("general.name") or "", "size_label": meta.get("general.size_label") or "",
        "ftype": FTYPE.get(meta.get("general.file_type"), str(meta.get("general.file_type") or "?")),
        "n_layer": n_layer, "n_embd": n_embd, "n_head": n_head or 0,
        "n_head_kv": _arr(hkv) if isinstance(hkv, dict) else (hkv if hkv is not None else n_head),
        "head_k": int(head_k or 0), "head_v": int(head_v or 0), "ctx_train": int(g("context_length", 0) or 0),
        "head_k_swa": int(g("attention.key_length_swa", 0) or 0), "head_v_swa": int(g("attention.value_length_swa", 0) or 0),
        "swa": int(swa or 0), "swa_pattern": pattern, "shared_kv": int(g("attention.shared_kv_layers", 0) or 0),
        "vocab": int(vocab or 0), "template": bool(meta.get("tokenizer.chat_template")),
        "experts": int(g("expert_count", 0) or 0), "experts_used": int(g("expert_used_count", 0) or 0),
        "split": int(meta.get("split.count") or 0), "vision": bool(meta.get("clip.has_vision_encoder")),
    }


class GGUFCache:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.data = read_json(path, {}) if path else {}
        self.lock = threading.Lock()

    def facts(self, model_file: str) -> Optional[dict]:
        ff = file_facts(model_file)
        if not ff["have"]:
            return None
        first = model_parts(model_file)[0]
        key = f"{first}|{ff['size']}|{int(ff['mtime'] or 0)}"
        with self.lock:
            hit = self.data.get(key)
        if hit:
            return hit
        try:
            facts = model_facts(read_gguf(first))
        except Exception as e:
            facts = {"error": f"{type(e).__name__}: {e}"}
        with self.lock:
            self.data[key] = facts
            if self.path:
                try:
                    atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=0))
                except OSError:
                    pass
        return facts


# ---------------------------------------------------------------- hardware
def ram_info() -> dict:
    total = avail = None
    try:
        if sys.platform == "darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^(Pages [^:]+):\s+(\d+\.?)", vm, re.M)}
            avail = page * sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
        elif os.path.exists("/proc/meminfo"):
            mi = dict(re.findall(r"^(\w+):\s+(\d+)", open("/proc/meminfo").read(), re.M))
            total, avail = int(mi["MemTotal"]) * 1024, int(mi.get("MemAvailable", mi.get("MemFree", 0))) * 1024
        elif WINDOWS or os.environ.get("MSYSTEM"):
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "$o=Get-CimInstance Win32_OperatingSystem; \"$($o.TotalVisibleMemorySize) $($o.FreePhysicalMemory)\""],
                                 capture_output=True, text=True, timeout=20).stdout.split()
            total, avail = int(out[0]) * 1024, int(out[1]) * 1024
    except Exception:
        pass
    cpu = ""
    try:
        if sys.platform == "darwin":
            cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5).stdout.strip()
        elif os.path.exists("/proc/cpuinfo"):
            m = re.search(r"model name\s*:\s*(.+)", open("/proc/cpuinfo").read())
            cpu = m.group(1).strip() if m else ""
    except Exception:
        pass
    return {"total": total, "avail": avail, "cpu": cpu, "cores": os.cpu_count()}


DEV_RE = re.compile(r"^\s*([A-Za-z]+\d*):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)")


def accel_dir(accel: str) -> str:
    a = (accel or "CPU").upper()
    sub = {"GPU": "gmlama-vulkan", "CPU": "gmlama", "": "gmlama"}.get(a, "gmlama-" + a.lower())
    return os.path.join(local_bin(), sub)


def server_bin(accel: str, version: str = "") -> str:
    d = accel_dir(accel)
    if version:
        d = os.path.join(d, version)
    return os.path.join(d, "llama-server" + EXE)


class Hardware:
    """RAM and the devices each llama-server build sees (--list-devices, cached; free memory changes as services
    load, so a launch or ===hw looks again)."""

    _shared = None

    @classmethod
    def shared(cls) -> "Hardware":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def __init__(self, ttl: float = 600.0):
        self.ttl = ttl
        self.devs: Dict[str, dict] = {}  # bin -> {"at", "devices", "version", "err"}
        self.ram = {"at": 0}
        self.lock = threading.Lock()

    def memory(self, max_age: float = 15.0) -> dict:
        if time.time() - self.ram.get("at", 0) > max_age:
            r = ram_info()
            r["at"] = time.time()
            self.ram = r
        return self.ram

    def devices(self, binp: str, max_age: Optional[float] = None) -> dict:
        max_age = self.ttl if max_age is None else max_age
        with self.lock:
            hit = self.devs.get(binp)
        if hit and time.time() - hit["at"] < max_age:
            return hit
        res = {"at": time.time(), "devices": [], "version": "", "err": ""}
        if not os.path.exists(binp):
            res["err"] = "no llama-server here"
        else:
            try:
                p = subprocess.run([binp, "--list-devices"], cwd=os.path.dirname(binp), capture_output=True,
                                   text=True, timeout=40)
                for line in (p.stdout + "\n" + p.stderr).splitlines():
                    m = DEV_RE.match(line)
                    if m and not m.group(1).upper().startswith("BLAS") and not m.group(1).upper().startswith("CPU"):
                        res["devices"].append({"name": m.group(1), "desc": m.group(2), "total": int(m.group(3)) * 2 ** 20,
                                               "free": int(m.group(4)) * 2 ** 20})
                v = subprocess.run([binp, "--version"], cwd=os.path.dirname(binp), capture_output=True, text=True,
                                   timeout=20)
                m = re.search(r"build (\d+)", v.stdout + v.stderr)
                res["version"] = f"b{m.group(1)}" if m else ""
            except Exception as e:
                res["err"] = f"{type(e).__name__}: {e}"
        with self.lock:
            self.devs[binp] = res
        return res

    def forget(self):
        with self.lock:
            for v in self.devs.values():
                v["at"] = 0
        self.ram["at"] = 0


# ---------------------------------------------------------------- capacity
CACHE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32, "q4_1": 20 / 32,
               "q5_0": 22 / 32, "q5_1": 24 / 32, "iq4_nl": 18 / 32}


def estimate(facts: Optional[dict], weights: int, pairs: List[list], accel: str, devs: List[dict], ram: dict,
             mmproj_size: int = 0) -> dict:
    """What a launch needs (weights, KV cache, compute, projector) on the GPU and in RAM, against what is free."""
    f = facts or {}
    n_layer = int(f.get("n_layer") or 0)
    ctx = pget(pairs, "ctx")
    try:
        n_ctx = int(ctx) if ctx not in (None, True) else 0
    except ValueError:
        n_ctx = 0
    ctx_note = ""
    if n_ctx <= 0:
        n_ctx = int(f.get("ctx_train") or 4096)
        ctx_note = f"no -c: the model's {n_ctx}"
    ub = int(pget(pairs, "ubatch") or 512) if str(pget(pairs, "ubatch") or "512").isdigit() else 512
    bk = CACHE_BYTES.get(str(pget(pairs, "ctk") or "f16").lower(), 2.0)
    bv = CACHE_BYTES.get(str(pget(pairs, "ctv") or "f16").lower(), 2.0)
    swa, swafull = int(f.get("swa") or 0), bool(pget(pairs, "swafull"))
    pattern = f.get("swa_pattern") or []
    hkv = f.get("n_head_kv")
    kv = 0.0
    kv_layers = max(0, n_layer - int(f.get("shared_kv") or 0))
    swa_layers = 0
    for i in range(kv_layers):
        h = hkv[i] if isinstance(hkv, list) and i < len(hkv) else (hkv if not isinstance(hkv, list) else 0)
        local = bool(pattern[i]) if i < len(pattern) else (swa > 0 and f.get("arch", "").startswith("gemma") and (i + 1) % 6 != 0)
        if local and swa and not swafull:
            swa_layers += 1
            cells = min(n_ctx, swa + ub)
        else:
            cells = n_ctx
        hk = (f.get("head_k_swa") or f.get("head_k", 0)) if local else f.get("head_k", 0)
        hv = (f.get("head_v_swa") or f.get("head_v", 0)) if local else f.get("head_v", 0)
        kv += cells * float(h or 0) * (hk * bk + hv * bv)
    fa = str(pget(pairs, "fa") or "auto").lower()
    compute = f.get("vocab", 32000) * ub * 4 + 64 * 2 ** 20
    if fa in ("off", "0", "false"):
        compute += int(f.get("n_head") or 8) * n_ctx * ub * 4
    gpu_build = (accel or "CPU").upper() != "CPU" and bool(devs)
    frac = ngl_fraction(pget(pairs, "ngl"), n_layer) if gpu_build else 0.0
    kv_gpu = gpu_build and frac > 0 and not pget(pairs, "nkvo")
    mm = pget(pairs, "mmproj")
    mm_size = mmproj_size if mm else 0
    mm_gpu = gpu_build and frac > 0 and not pget(pairs, "nommproj")
    gpu_need = weights * frac + (kv if kv_gpu else 0) + (compute if gpu_build and frac > 0 else 0) + (mm_size if mm_gpu else 0)
    ram_need = weights * (1 - frac) + (0 if kv_gpu else kv) + (0 if gpu_build and frac > 0 else compute) + (0 if mm_gpu else mm_size)
    want = str(pget(pairs, "dev") or "").split(",")[0].strip()
    dev = next((d for d in devs if want and d["name"].lower() == want.lower()), None) or (devs[0] if devs else None)
    out = {"n_ctx": n_ctx, "ctx_note": ctx_note, "kv": kv, "compute": compute, "weights": weights, "mmproj": mm_size,
           "frac": frac, "gpu_need": gpu_need, "ram_need": ram_need, "device": dev, "swa_layers": swa_layers,
           "kv_layers": kv_layers, "verdict": "?", "notes": []}
    notes = out["notes"]
    if want and devs and not any(d["name"].lower() == want.lower() for d in devs):
        notes.append(("bad", f"device {want} is not one this build sees ({', '.join(d['name'] for d in devs)})"))
    verdict = "ok"
    if gpu_build and dev and frac > 0:
        free = dev["free"]
        if gpu_need > free:
            over = gpu_need - free
            per_layer = (weights / max(1, n_layer + 1)) or 1
            verdict = "over"
            notes.append(("warn", f"{dev['name']} needs {gib(gpu_need)} of {gib(free)} free: about "
                                  f"{min(n_layer, int(over / per_layer) + 1)} layers stay on the CPU (-fit) or it fails"))
        elif gpu_need > free * 0.9:
            verdict = "tight"
            notes.append(("warn", f"{dev['name']} needs {gib(gpu_need)} of {gib(free)} free: tight"))
    elif (accel or "CPU").upper() != "CPU" and not devs:
        notes.append(("warn", "this build sees no GPU: it runs on the CPU"))
    avail = ram.get("avail")
    if avail:
        if ram_need > avail:
            verdict = "no" if ram_need > (ram.get("total") or avail) * 0.95 else "over"
            notes.append(("bad" if verdict == "no" else "warn", f"RAM needs {gib(ram_need)} of {gib(avail)} available"))
        elif ram_need > avail * 0.85 and verdict == "ok":
            verdict = "tight"
    if n_ctx > (f.get("ctx_train") or n_ctx):
        notes.append(("warn", f"-c {n_ctx} is beyond the model's training context {f.get('ctx_train')}"))
    out["verdict"] = verdict
    return out


# ---------------------------------------------------------------- processes, ports, probes
def port_free(port: int) -> bool:
    """Can a llama-server listen on the port now? It binds with SO_REUSEADDR, so the connections a server that has just
    stopped leaves in TIME_WAIT (~30 s on macOS; gml8's own probes make them) do not count — a live listener does."""
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            if not WINDOWS:  # on Windows SO_REUSEADDR would let the bind steal a port in use
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def port_holder(port: int) -> Optional[dict]:
    if WINDOWS:
        return None
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pid = re.search(r"^p(\d+)", out, re.M)
    cmd = re.search(r"^c(.+)", out, re.M)
    return {"pid": int(pid.group(1)), "cmd": cmd.group(1) if cmd else ""} if pid else None


def wait_port_free(port: Optional[int], limit: float = 2.0) -> float:
    """Wait (up to limit seconds) for a port a stopped service held to be free again; the seconds it took."""
    t0 = time.time()
    if not port:
        return 0.0
    while not port_free(int(port)) and time.time() - t0 < limit:
        time.sleep(0.1)
    return time.time() - t0


def free_port(start: int = 8080, avoid=(), limit: int = 200) -> Optional[int]:
    for p in range(start, start + limit):
        if p not in avoid and port_free(p):
            return p
    return None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is gone
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def proc_cmdline(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}


def llama_processes() -> List[dict]:
    """Every llama-server running here (whoever started it): pid, model, port."""
    if WINDOWS:
        return []
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,etime=,command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+([\d:-]+)\s+(.*)$", line)
        if not m:
            continue
        cmd = m.group(4)
        et = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", m.group(3))
        secs = (int(et.group(1) or 0) * 86400 + int(et.group(2) or 0) * 3600 + int(et.group(3)) * 60 + int(et.group(4))) if et else 0
        argv0 = cmd.split(" ", 1)[0]
        if not os.path.basename(argv0).startswith("llama-server") and "/llama-server " not in cmd:
            continue
        if os.path.basename(argv0).lstrip("-") in SHELLS:
            continue  # a shell that runs one ("bash -c './llama-server …'"): its llama-server is listed by itself
        toks = cmd.split()
        if any(t in ("--list-devices", "--version", "--help", "-h") for t in toks[1:]):
            continue  # a probe (gml8's own --list-devices), not a server
        pairs = parse_params(toks[1:])
        router = "--models-preset" in toks or "--models-dir" in toks
        if not pget(pairs, "model") and not router:
            continue
        port = pget(pairs, "port")
        res.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "cmd": cmd, "t0": time.time() - secs,
                    "model": str(pget(pairs, "model") or ("(router: " + str(pget(pairs, "--models-preset") or "models") + ")")),
                    "port": int(port) if str(port or "").isdigit() else 8080, "host": str(pget(pairs, "host") or "127.0.0.1")})
    return res


def http_get(port: int, path: str, timeout: float = 2.0, host: str = "127.0.0.1"):
    url = f"http://{host}:{port}{path}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer gml8"}), timeout=timeout) as r:
            body = r.read(200000).decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except ValueError:
                data = body
            return r.status, data, "", int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(20000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, str(e.reason), int((time.time() - t0) * 1000)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        kind = "refused" if "refused" in str(reason).lower() else "timeout" if "timed out" in str(reason).lower() else "error"
        return None, None, kind, int((time.time() - t0) * 1000)


def probe(port: int, timeout: float = 2.0, host: str = "127.0.0.1") -> dict:
    """The OpenAI API probe: GET /v1/models. up (200, the model id) · loading (503) · refused · timeout · error."""
    code, data, err, ms = http_get(port, "/v1/models", timeout, host)
    if code == 200:
        model = ""
        if isinstance(data, dict):
            items = data.get("data") or data.get("models") or []
            if items and isinstance(items[0], dict):
                model = items[0].get("id") or items[0].get("name") or items[0].get("model") or ""
        return {"state": "up", "code": 200, "model": model, "ms": ms}
    if code == 503:
        return {"state": "loading", "code": 503, "ms": ms}
    if code is not None:
        return {"state": "error", "code": code, "err": clip(data or err, 160), "ms": ms}
    return {"state": err, "code": None, "ms": ms}


def server_props(port: int, timeout: float = 2.0) -> dict:
    code, data, _, _ = http_get(port, "/props", timeout)
    if code != 200 or not isinstance(data, dict):
        return {}
    dgs = data.get("default_generation_settings") or {}
    mods = data.get("modalities") or {}
    return {"n_ctx": dgs.get("n_ctx") or data.get("n_ctx"), "vision": bool(mods.get("vision")),
            "audio": bool(mods.get("audio")), "slots": data.get("total_slots"), "build": data.get("build_info") or "",
            "model_path": data.get("model_path") or ""}


# ---------------------------------------------------------------- logs
SPEED = re.compile(r"(prompt eval|eval) time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens.*?([\d.]+) tokens per second")
LOG_RULES = [
    (r"couldn'?t bind|address already in use|bind\(\) failed|failed to bind", "port",
     "the port is taken: another process listens there", "stop it or use another --port (reserved entries: gml8 picks a free one)"),
    (r"out of (device )?memory|ErrorOutOfDeviceMemory|failed to allocate|unable to allocate|cudaMalloc failed|"
     r"insufficient memory|OutOfMemory|failed to create context|vk::Device::allocateMemory", "memory",
     "not enough memory (GPU or RAM) for the weights + KV cache",
     "lower -c, quantize the KV cache (-ctk q8_0 -ctv q8_0 / q4_0), fewer -ngl layers, -fit on, drop -mm or --no-mmproj-offload"),
    (r"unknown model architecture|unsupported model architecture", "version",
     "this llama.cpp build does not know the model's architecture", "use a newer llama.cpp (the entry's version field)"),
    (r"error while handling argument|invalid argument|unknown argument|unrecognized (option|argument)|error: invalid", "args",
     "a parameter this llama.cpp build does not accept", "fix or drop the flag, or use the version that knows it"),
    (r"failed to load model|error loading model|gguf_init_from_file|failed to open .*gguf|No such file", "model",
     "the model file cannot be loaded (missing, partial download or corrupt)", "check the file under $GMLBASE (artefact-get / import)"),
    (r"mtmd_init_from_file|clip_model_load|failed to load (the )?(mmproj|multimodal)", "mmproj",
     "the vision projector (-mm) cannot be loaded", "check the mmproj file matches the model"),
    (r"no usable GPU|No devices found|failed to initialize .* backend|invalid device|device .* not found|"
     r"unknown device", "device", "the GPU device is wrong or missing", "check -dev / the device field (gmlvk-dev lists them)"),
    (r"Library not loaded|dyld|cannot open shared object|image not found", "bin",
     "the llama.cpp build is broken (a library is missing)", "re-extract the build (artefact-ready <version>)"),
    (r"Killed: 9|killed by signal 9|SIGKILL", "killed", "the process was killed (out of memory?)", "lower the memory it needs"),
]


def log_tail(path: Optional[str], n: int = 120, max_bytes: int = 400000) -> List[str]:
    if not path:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def log_facts(lines: List[str]) -> dict:
    out = {"tps_gen": None, "tps_prompt": None, "listening": False, "errors": []}
    for l in lines:
        m = SPEED.search(l)
        if m:
            out["tps_prompt" if m.group(1) == "prompt eval" else "tps_gen"] = float(m.group(4))
        if "server is listening" in l or "starting the main loop" in l:
            out["listening"] = True
    out["errors"] = diagnose_lines(lines)
    return out


def diagnose_lines(lines: List[str]) -> List[dict]:
    found = []
    for rx, kind, cause, fix in LOG_RULES:
        r = re.compile(rx, re.I)
        ev = [l.strip() for l in lines if r.search(l)]
        if ev:
            found.append({"kind": kind, "cause": cause, "fix": fix, "evidence": clip(ev[-1], 220)})
    return found


# ---------------------------------------------------------------- memory
class Memory:
    def __init__(self, base: str = DEFAULT_MEMORY, host: Optional[str] = None):
        self.base = os.path.expanduser(base)
        self.root = os.path.join(self.base, host or hostname())
        self.run_dir = os.path.join(self.root, "run")
        self.logs_dir = os.path.join(self.root, "logs")
        self.bak_dir = os.path.join(self.root, "inventory.bak")
        for d in (self.run_dir, self.logs_dir, self.bak_dir):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.gguf = GGUFCache(os.path.join(self.root, "gguf.json"))
        self.entries_path = os.path.join(self.root, "entries.json")
        self.entry_data = read_json(self.entries_path, {})

    def event(self, ev: str, **kw):
        rec = {"at": utc_now(), "ev": ev}
        rec.update({k: v for k, v in kw.items() if v not in (None, "", [], {})})
        with self.lock:
            with open(os.path.join(self.root, "events.jsonl"), "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def events(self, key: Optional[str] = None, n: int = 20, kinds=None) -> List[dict]:
        out = []
        try:
            with open(os.path.join(self.root, "events.jsonl"), "rb") as fb:
                fb.seek(0, 2)
                size = fb.tell()
                fb.seek(max(0, size - 2_000_000))  # the recent past is enough (the file grows for months)
                data = fb.read().decode("utf-8", "replace")
        except OSError:
            return out
        for line in data.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if key and r.get("key") != key:
                continue
            if kinds and r.get("ev") not in kinds:
                continue
            out.append(r)
        return out[-n:]

    def backup(self, path: str):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        atomic_write(os.path.join(self.bak_dir, f"{os.path.basename(path)}.{stamp()}"), text)
        baks = sorted(n for n in os.listdir(self.bak_dir) if n.startswith(os.path.basename(path) + "."))
        for old in baks[:-50]:
            try:
                os.remove(os.path.join(self.bak_dir, old))
            except OSError:
                pass

    def entry(self, key: str) -> dict:
        with self.lock:
            return dict(self.entry_data.get(key) or {})

    def update_entry(self, key: str, fn):
        with self.lock:
            d = self.entry_data.setdefault(key, {})
            fn(d)
            try:
                atomic_write(self.entries_path, json.dumps(self.entry_data, ensure_ascii=False, indent=1))
            except OSError:
                pass

    def note(self, key: str, text: str):
        self.update_entry(key, lambda d: d.setdefault("notes", []).append({"at": utc_now(), "text": clip(text, 400)}))
        self.event("note", key=key, text=clip(text, 300))

    def services_path(self) -> str:
        return os.path.join(self.run_dir, "services.json")

    def write_services(self, services: List[dict]):
        atomic_write(self.services_path(), json.dumps({"at": utc_now(), "host": hostname(), "pid": os.getpid(),
                                                       "services": services}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- launching
class Spec:
    """Everything a launch needs, built from an entry (or its staged values)."""

    def __init__(self):
        self.key = self.model = self.model_abs = self.bin = self.cwd = self.params = self.profile = ""
        self.version = self.device = self.script = self.pre = self.post = ""
        self.line = 0
        self.port = 0
        self.cmd: List[str] = []
        self.env: Dict[str, str] = {}
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.adapted: List[str] = []
        self.missing_vars: List[str] = []
        self.wait = 300.0
        self.adapt_set: Dict[str, str] = {}  # flags gml8 changed to fit this machine (reserved entries)
        self.adapt_profile = None  # a placeholder gets a profile
        self.reserved = True
        self.kind = "chat"
        self.vision = False

    def shell(self) -> str:
        parts = []
        for i, c in enumerate(self.cmd):
            if i == 0 and os.path.dirname(c) == self.cwd:
                c = "./" + os.path.basename(c)
            parts.append(quote(tilde(c)))
        return f"cd {quote(tilde(self.cwd))} && " + " ".join(parts)


def named_script(entry_or_name, extra: Optional[dict] = None) -> str:
    extra = extra or {}
    s = str(extra.get("script") or "")
    if s:
        p = os.path.expanduser(envsubst(s))
        return p if os.path.isabs(p) else os.path.join(NAMED_DIR, p)
    return ""


def build_spec(entry: Entry, values: Optional[dict] = None, avoid_ports=(), adapt: bool = True) -> Spec:
    """The launch of an entry: values = staged values (model, profile, params, version, device, extra) when given.
    Reserved entries are adapted (a free port); named ones are launched as written."""
    v = entry.values()
    if values:
        v.update({k: val for k, val in values.items() if val is not None})
    sp = Spec()
    sp.key, sp.line, sp.reserved = entry.key, entry.line, entry.reserved
    sp.model, sp.profile, sp.version = v["model"], v["profile"] or "", envsubst(v["version"] or "")
    extra = v.get("extra") or {}
    accel = sp.profile.split("/", 1)[0].upper() if "/" in sp.profile else "CPU"
    pname = sp.profile.split("/", 1)[1] if "/" in sp.profile else sp.profile
    missing: List[str] = []
    params = expand_params(v["params"], missing=missing)
    device = expand_params(v["device"], missing=missing).strip()
    if not device and accel != "CPU" and os.environ.get("GML_DEV"):
        device = os.environ["GML_DEV"]
    sp.missing_vars = missing
    for name in missing:
        sp.warnings.append(f"${name} is not set (empty, as envsubst does)")
    sp.model_abs = model_path(sp.model)
    sp.bin = server_bin(accel, sp.version)
    sp.cwd = os.path.dirname(sp.bin)
    if not file_facts(sp.model_abs)["exists"]:
        sp.errors.append(f"the model file is not here: {tilde(sp.model_abs)}")
    pairs = parse_params(params)
    pairs = [p for p in pairs if not (p[0] and ALIAS.get(p[0]) == "model")]  # -m is the entry's model
    if entry.reserved and adapt and entry.kind == "placeholder" and not (values and (values.get("params") or values.get("profile"))):
        # a placeholder launched for the first time: a profile and a context size that fit this machine
        gpu = Hardware.shared().devices(server_bin("GPU", sp.version))["devices"]
        if gpu and not sp.profile:
            sp.profile, accel = "GPU/", "GPU"
            sp.adapt_profile = "GPU/"
            sp.adapted.append(f"profile GPU/ (this machine has {gpu[0]['name']} {gpu[0]['desc']})")
            sp.bin = server_bin(accel, sp.version)
            sp.cwd = os.path.dirname(sp.bin)
        if pget(pairs, "ctx") is None:
            pairs = pset(pairs, "-c", 8192)
            sp.adapt_set["-c"] = "8192"
            sp.adapted.append("-c 8192 (the model's own context would take a large KV cache)")
    if device:
        pairs = pset(pairs, "-dev", device)
    port = pget(pairs, "port")
    port_n = int(port) if str(port or "").isdigit() else None
    explicit = extra.get("port")
    if explicit and str(explicit).isdigit():
        port_n = int(explicit)
    taken = lambda p: (p in avoid_ports) or not port_free(p)  # noqa: E731
    if entry.reserved and adapt:
        if port_n is None:
            new = free_port(8080, avoid_ports)
            sp.adapted.append(f"--port {new} (the entry has none)")
            pairs = pset(pairs, "--port", new)
            sp.adapt_set["--port"] = str(new)
            port_n = new
        elif taken(port_n):
            holder = port_holder(port_n)
            new = free_port(port_n + 1, avoid_ports)
            sp.adapted.append(f"--port {port_n} → {new} ({port_n} is held by "
                              + (f"pid {holder['pid']} {holder['cmd']}" if holder else "another service") + ")")
            pairs = pset(pairs, "--port", new)
            sp.adapt_set["--port"] = str(new)
            port_n = new
    else:
        if port_n is None:
            port_n = 8080
        if taken(port_n):
            holder = port_holder(port_n)
            sp.errors.append(f"port {port_n} is taken" + (f" by pid {holder['pid']} ({holder['cmd']})" if holder else "")
                             + " — a named entry is launched as written: stop that, or change its --port")
    sp.port = port_n or 8080
    sp.params = join_params(pairs)
    sp.device = device
    if pget(pairs, "embed"):
        sp.kind = "embed"
    elif pget(pairs, "rerank"):
        sp.kind = "rerank"
    sp.vision = bool(pget(pairs, "mmproj"))
    mm = pget(pairs, "mmproj")
    if mm and mm is not True and not os.path.exists(os.path.expanduser(str(mm))):
        sp.errors.append(f"the projector -mm is not here: {tilde(str(mm))}")
    sp.env = {str(k): envsubst(str(val)) for k, val in ((extra.get("env") or {}).items() if isinstance(extra.get("env"), dict) else [])}
    sp.pre, sp.post = str(extra.get("pre") or ""), str(extra.get("post") or "")
    try:
        sp.wait = float(extra.get("wait") or 300)
    except (TypeError, ValueError):
        sp.wait = 300.0
    script = named_script(entry, extra)
    if not script and not entry.reserved and pname and not v["params"]:
        cand = os.path.join(NAMED_DIR, pname)
        if os.path.exists(cand):
            script = cand
    if script:
        if not os.access(script, os.X_OK):
            sp.errors.append(f"the script is not executable: {tilde(script)}")
        sp.script = script
        sp.cwd = GMLAMA
        sp.cmd = [script]
    else:
        if not os.path.exists(sp.bin):
            sp.errors.append(f"no llama-server for {accel}{('/' + sp.version) if sp.version else ''}: {tilde(sp.bin)}")
        sp.cmd = [sp.bin, "-m", sp.model_abs] + split_params(sp.params)
    return sp


def adapted_line(entry: Entry, sp: Spec) -> Optional[str]:
    """The entry's line with what gml8 adapted (reserved entries): the raw params keep their $VARs."""
    if not (sp.adapt_set or sp.adapt_profile):
        return None
    pairs = parse_params(entry.params)
    for flag, val in sp.adapt_set.items():
        pairs = pset(pairs, flag, val)
    return format_entry(entry.model, sp.adapt_profile or entry.profile, join_params(pairs), entry.version, entry.device,
                        None, entry.comment)


def spawn(spec: Spec, log_path: str) -> subprocess.Popen:
    """Start the service detached from gml8 (its own session): it keeps running after gml8 and Neovim stop."""
    env = dict(os.environ)
    env.update(spec.env)
    env.update({"GML8_BIN": spec.bin, "GML8_MODEL": spec.model_abs, "GML8_PARAMS": spec.params, "GML8_DEV": spec.device,
                "GML8_PORT": str(spec.port), "GML8_LOG": log_path, "GML8_KEY": spec.key, "GML8_CWD": os.path.dirname(spec.bin),
                "GML8_NAME": spec.profile})
    logf = open(log_path, "ab", buffering=0)
    logf.write((f"# gml8 {utc_now()} launch {spec.key} :{spec.port}\n# $ {spec.shell()}\n").encode())
    if spec.env:
        logf.write(("# env " + " ".join(f"{k}={v}" for k, v in spec.env.items()) + "\n").encode())
    slots = pget(parse_params(spec.params), "slotsave")  # llama-server refuses a --slot-save-path that is no directory
    if slots and slots is not True and not os.path.isdir(str(slots)):
        try:
            os.makedirs(str(slots), exist_ok=True)
            logf.write(f"# created the --slot-save-path directory {slots}\n".encode())
        except OSError as ex:
            logf.write(f"# could not create the --slot-save-path directory {slots}: {ex}\n".encode())
    if spec.pre:
        logf.write(f"# pre: {spec.pre}\n".encode())
        r = subprocess.run(spec.pre, shell=True, cwd=spec.cwd, env=env, stdout=logf, stderr=subprocess.STDOUT, timeout=300)
        if r.returncode != 0:
            logf.close()
            raise RuntimeError(f"pre failed (exit {r.returncode}): {spec.pre}")
    kw = {}
    if WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(spec.cmd, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, close_fds=True, **kw)
    finally:
        logf.close()


def terminate(pid: int, group: bool = True, wait: float = 10.0) -> str:
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return "killed"
    sig_send = (lambda s: os.killpg(pid, s)) if group else (lambda s: os.kill(pid, s))
    try:
        sig_send(signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return "not allowed"
    end = time.time() + wait
    while time.time() < end:
        if not pid_alive(pid):
            return "stopped"
        time.sleep(0.2)
    try:
        sig_send(signal.SIGKILL)
    except OSError:
        pass
    return "killed"


class Runs:
    """Every service gml8 launched (run/runs.json) and every llama-server it found running (foreign)."""

    def __init__(self, mem: Memory):
        self.mem = mem
        self.path = os.path.join(mem.run_dir, "runs.json")
        self.runs: Dict[str, dict] = read_json(self.path, {})
        self.procs: Dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()

    def save(self):
        with self.lock:
            keep = {}
            done = sorted((r for r in self.runs.values() if r.get("state") not in ACTIVE),
                          key=lambda r: r.get("started") or "")[-60:]
            for r in self.runs.values():
                if r.get("state") in ACTIVE or r in done:
                    keep[r["id"]] = r
            self.runs = keep
            atomic_write(self.path, json.dumps(self.runs, ensure_ascii=False, indent=1))

    def active(self) -> List[dict]:
        with self.lock:
            return [r for r in self.runs.values() if r.get("state") in ACTIVE]

    def of_key(self, key: str) -> Optional[dict]:
        """The run of an entry: the active one, else the latest."""
        with self.lock:
            rs = [r for r in self.runs.values() if r.get("key") == key]
        act = [r for r in rs if r.get("state") in ACTIVE]
        pool = act or rs
        return max(pool, key=lambda r: r.get("started") or "") if pool else None

    def of_port(self, port: int) -> Optional[dict]:
        with self.lock:
            return next((r for r in self.runs.values() if r.get("port") == port and r.get("state") in ACTIVE), None)

    def start(self, spec: Spec, staged: bool = False, origin: str = "user") -> dict:
        safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", spec.key.lstrip("#@"))
        log = os.path.join(self.mem.logs_dir, f"{safe}-{spec.port}-{stamp()}.log")
        proc = spawn(spec, log)
        rid = f"{safe}-{spec.port}-{stamp()}"
        run = {"id": rid, "key": spec.key, "line": spec.line, "model": spec.model, "model_abs": spec.model_abs,
               "port": spec.port, "pid": proc.pid, "cmd": spec.cmd, "shell": spec.shell(), "cwd": spec.cwd, "log": log,
               "params": mask(spec.params), "profile": spec.profile, "version": spec.version, "device": spec.device,
               "started": utc_now(), "t0": time.time(), "state": "launching", "staged": staged, "origin": "gml8",
               "by": origin, "kind": spec.kind, "vision": spec.vision, "wait": spec.wait, "post": spec.post,
               "adapted": spec.adapted, "script": spec.script}
        with self.lock:
            self.runs[rid] = run
            self.procs[rid] = proc
        self.save()
        self.mem.event("launch", key=spec.key, line=spec.line, port=spec.port, pid=proc.pid, model=spec.model,
                       params=mask(spec.params), staged=staged or None, by=origin, adapted=spec.adapted, log=log)
        self.mem.update_entry(spec.key, lambda d: d.update(launches=d.get("launches", 0) + 1, last_launch=utc_now(),
                                                            model=spec.model))
        return run

    def alive(self, run: dict) -> bool:
        proc = self.procs.get(run["id"])
        if proc is not None:
            code = proc.poll()
            if code is not None:
                run["code"] = code
                return False
            return True
        if not pid_alive(run.get("pid")):
            return False
        cl = proc_cmdline(run["pid"]) if not WINDOWS else ""
        return not cl or "llama-server" in cl or bool(run.get("script")) or run.get("origin") == "foreign"

    def stop(self, run: dict, why: str = "") -> str:
        run["stop_requested"] = True
        prev = run.get("state")
        run["state"] = "stopping"
        own = run.get("origin") == "gml8"
        res = terminate(run["pid"], group=own)
        proc = self.procs.pop(run["id"], None)
        if proc is not None:
            try:
                run["code"] = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        run["state"] = "stopped"
        run["ended"] = utc_now()
        self.save()
        self.mem.event("stop", key=run.get("key"), port=run.get("port"), pid=run.get("pid"), result=res, why=why,
                       was=prev)
        return res

    def adopt(self, procs: List[dict], inv: Inventory) -> List[dict]:
        """llama-servers running here that no run knows: recorded as foreign runs of the entry they match."""
        new = []
        with self.lock:
            known = {r.get("pid") for r in self.runs.values() if r.get("state") in ACTIVE}
        for p in procs:
            if p["pid"] in known:
                continue
            ent = match_entry(inv, p["model"], p["port"])
            key = ent.key if ent else f"pid{p['pid']}"
            rid = f"foreign-{p['pid']}"
            run = {"id": rid, "key": key, "line": ent.line if ent else 0, "model": ent.model if ent else p["model"],
                   "model_abs": p["model"], "port": p["port"], "pid": p["pid"], "cmd": p["cmd"].split(), "shell": p["cmd"],
                   "log": None, "params": mask(" ".join(p["cmd"].split()[1:])), "started": utc_now(p.get("t0")),
                   "t0": p.get("t0") or time.time(),
                   "state": "launching", "origin": "foreign", "by": "outside gml8", "kind": "chat",
                   "vision": " -mm " in f" {p['cmd']} " or "--mmproj" in p["cmd"]}
            if "--embedding" in p["cmd"]:
                run["kind"] = "embed"
            with self.lock:
                self.runs[rid] = run
            new.append(run)
            self.mem.event("found", key=key, port=p["port"], pid=p["pid"], cmd=clip(mask(p["cmd"]), 300))
        if new:
            self.save()
        return new


def match_entry(inv: Inventory, model: str, port: int) -> Optional[Entry]:
    if not model:
        return None
    absm = os.path.realpath(model) if os.path.exists(model) else model
    cands = [e for e in inv.entries() if os.path.realpath(model_path(e.model)) == absm or model.endswith(e.model)]
    if not cands:
        return None
    for e in cands:
        p = pget(parse_params(envsubst(e.params)), "port")
        if str(p) == str(port):
            return e
    return cands[0]


# ---------------------------------------------------------------- discovery (for other agents and scripts)
def read_services(base: str = DEFAULT_MEMORY, host: Optional[str] = None) -> dict:
    return read_json(os.path.join(os.path.expanduser(base), host or hostname(), "run", "services.json"), {})


def rank(services: List[dict], kind="chat", vision=None, min_ctx=0, name=None, model=None, port=None) -> List[dict]:
    out = []
    for s in services:
        if s.get("state") != "up":
            continue
        if port and int(s.get("port") or 0) != int(port):
            continue
        if kind and s.get("kind", "chat") != kind:
            continue
        if vision and not s.get("vision"):
            continue
        if min_ctx and (s.get("n_ctx") or 0) < min_ctx:
            continue
        if name and name.lower() not in f"{s.get('key')} {s.get('name')} {s.get('profile')}".lower():
            continue
        if model and model.lower() not in str(s.get("model") or "").lower():
            continue
        out.append(s)
    return sorted(out, key=lambda s: (-(1 if s.get("power") else 0), -(s.get("n_ctx") or 0), s.get("started") or ""))


def discover(kind="chat", vision=None, min_ctx=0, name=None, model=None, verify=True, base=DEFAULT_MEMORY) -> Optional[dict]:
    for s in rank((read_services(base).get("services") or []), kind, vision, min_ctx, name, model):
        if not verify or probe(int(s["port"]), 1.5)["state"] == "up":
            return s
    return None


# ---------------------------------------------------------------- CLI
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="gml8 engine: the llama.cpp services of this machine")
    ap.add_argument("cmd", choices=["status", "entries", "entry", "merge", "launch", "stop", "logs", "diagnose", "services",
                                    "discover", "gguf", "hw", "history"])
    ap.add_argument("ref", nargs="?")
    ap.add_argument("--inventory", default=os.environ.get("GML8_INVENTORY") or default_inventory())
    ap.add_argument("--series", default=default_series())
    ap.add_argument("--memory", default=os.environ.get("GML8_HOME") or DEFAULT_MEMORY)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--params")
    ap.add_argument("--port", type=int)
    ap.add_argument("--kind", default="chat")
    ap.add_argument("--vision", action="store_true")
    ap.add_argument("--min-ctx", type=int, default=0)
    ap.add_argument("--name")
    ap.add_argument("--model")
    ap.add_argument("--port-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.cmd == "discover":
        s = discover(a.kind, a.vision or None, a.min_ctx, a.name, a.model, base=a.memory)
        if not s:
            sys.exit(1)
        print(s["port"] if a.port_only else json.dumps(s, indent=1))
        return
    if a.cmd == "services":
        print(json.dumps(read_services(a.memory), indent=1))
        return
    if a.cmd == "gguf":
        print(json.dumps(model_facts(read_gguf(model_path(a.ref))), indent=1))
        return
    if a.cmd == "hw":
        hw = Hardware.shared()
        print(json.dumps({"ram": hw.memory(), "builds": {tilde(server_bin(x)): hw.devices(server_bin(x))
                                                          for x in ("CPU", "GPU")}}, indent=1, default=str))
        return
    mem = Memory(a.memory)
    inv = Inventory(a.inventory, a.series)
    if a.cmd == "merge":
        print(merge_text(merge_series(a.inventory, a.series, mem, dry=a.dry)) + (" (dry run)" if a.dry else ""))
        return
    if a.cmd == "entries":
        for e in inv.load():
            if e.kind in ("entry", "placeholder", "sep"):
                print(f"{e.line:4d} {e.key:24s} {e.kind:11s} {e.section:9s} {e.model} {e.profile}")
        return
    if a.cmd == "history":
        e = inv.find(a.ref) if a.ref else None
        for r in mem.events(e.key if e else None, 40):
            print(json.dumps(r, ensure_ascii=False))
        return
    runs = Runs(mem)
    if a.cmd == "status":
        for r in sorted(runs.runs.values(), key=lambda r: r.get("started") or ""):
            alive = runs.alive(r)
            pr = probe(r["port"], 1.5) if alive else {"state": "-"}
            print(f"{r['key']:20s} :{r['port']:<5d} pid {r['pid']:<7d} {'alive' if alive else 'gone':5s} "
                  f"{pr['state']:8s} {r.get('state'):9s} {when(r.get('started'))} {os.path.basename(str(r.get('model')))}")
        for p in llama_processes():
            print(f"  llama-server pid {p['pid']} :{p['port']} {p['model']}")
        return
    e = inv.find(a.ref)
    if a.cmd in ("stop", "logs", "diagnose") and not e and str(a.ref or "").isdigit():
        r = runs.of_port(int(a.ref))
    else:
        r = runs.of_key(e.key) if e else None
    if a.cmd == "entry":
        if not e:
            sys.exit(f"no entry {a.ref}")
        print(json.dumps(e.brief(), indent=1))
        sp = build_spec(e)
        print("$ " + sp.shell())
        for x in sp.errors + sp.warnings + sp.adapted:
            print("  " + x)
        return
    if a.cmd == "launch":
        if not e:
            sys.exit(f"no entry {a.ref}")
        vals = {"params": a.params} if a.params else None
        sp = build_spec(e, vals, avoid_ports={x["port"] for x in runs.active()})
        if a.port:
            sp.params = join_params(pset(parse_params(sp.params), "--port", a.port))
            sp.port = a.port
            sp.cmd = [sp.bin, "-m", sp.model_abs] + split_params(sp.params) if not sp.script else sp.cmd
        if sp.errors:
            sys.exit("\n".join(sp.errors))
        run = runs.start(sp, staged=bool(a.params), origin="cli")
        print(f"launched {run['key']} :{run['port']} pid {run['pid']} log {run['log']}")
        return
    if not r:
        sys.exit(f"no run for {a.ref}")
    if a.cmd == "stop":
        print(runs.stop(r, "cli"))
    elif a.cmd == "logs":
        print("\n".join(log_tail(r.get("log"), 60)))
    elif a.cmd == "diagnose":
        for d in diagnose_lines(log_tail(r.get("log"), 400)):
            print(f"{d['kind']}: {d['cause']} — {d['fix']}\n   {d['evidence']}")


if __name__ == "__main__":
    _cli()
