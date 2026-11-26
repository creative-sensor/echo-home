#!/usr/bin/env python
"""
ptrm_core: the engine of ptrm (port-remote), usable without the agent and from the shell.

ptrm puts services that run on remote (high-performance) hosts at ports of this machine: localhost:<LOCAL_PORT> is
forwarded to <REMOTE_IP>:<REMOTE_PORT> as seen from <SSH_HOST>. The entries are the rows of the user's ptrm.md table
(~/echo-home/.vim/pages/<hostname>.ptrm.md, else ptrm.md):

  | SSH_HOST                    | LOCAL_PORT | REMOTE_IP | REMOTE_PORT | NOTES    |
  | creativ@serpenti-a77g.local | 8081       | 127.0.0.1 | 8080        | llm/test |

NOTES starts with the service (kind/name: llm/gemma-4-12B …); llm/ rows are probed as LLM servers (/health, model).

  Table    read / add / change / remove rows (the table is re-aligned on write, the rest of the file is untouched)
  local    who listens on a local port (lsof / ss / netstat) and the ssh -L tunnels running here (ps)
  probe    what answers at localhost:<port>: closed · dead (a tunnel with nothing behind it) · open (silent) ·
           ok (+ HTTP code; the model for llm/ rows) · loading (HTTP 503)
  Tunnels  the ssh -N -L tunnels ptrm opens (detached: they outlive ptrm and Neovim), its registry run/tunnels.json
           ("want": keep it open, healed when its ssh dies), and the tunnels it finds (ptrmo terminals, other shells)
  remote   ssh to a host, BatchMode (keys only, never a password prompt): reach, run a script, discover (os, cpu,
           memory, gpu, tools, listening ports and the processes behind them, docker, services, launch recipes),
           launch a service detached (Windows hosts: outside the ssh session's job), stop it, health from there
  Memory   ~/echo-home/memory/ptrm: events.jsonl (every open/close/state change/discovery/launch, UTC),
           hosts/<host>.yaml (every host ever accessed), ports/<port>.yaml (every local port ever opened), run/

Every time is UTC: stored as 2026-11-25T06:40:12Z, shown as 2026-11-25 06:40Z.

Shell:  python ptrm_core.py [--table F] [--memory D] status | check <port> | open <port> | close <port> | tunnels
                           | reach <host> | discover <host> | hosts | history <port|host> | recall <words>
                           | note <port|host> <text> | listen
"""
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

IS_WIN = os.name == "nt"
HOME = os.environ.get("HOME") or os.path.expanduser("~")


def resolve_path(p: str) -> str:
    """~ and Git Bash/MSYS paths (/c/Users/..) to native ones (C:/Users/..)."""
    if not p:
        return p
    if p.startswith("~"):
        p = os.path.join(HOME, p[1:].lstrip("/\\"))
    if IS_WIN or os.environ.get("MSYSTEM"):
        m = re.match(r"^[\\/]([a-zA-Z])(?:[\\/]|$)(.*)", p)
        if m and not os.path.exists(p):
            p = f"{m.group(1).upper()}:/{m.group(2)}"
    if not re.match(r"^[a-zA-Z]:", p):
        p = os.path.abspath(p)
    return os.path.normpath(p).replace("\\", "/")


DEFAULT_MEMORY = "~/echo-home/memory/ptrm"
PAGES = "~/echo-home/.vim/pages"


# ---------------------------------------------------------------- time (UTC, always with the year)
def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def when(ts) -> str:
    """2026-11-25T06:40:12Z -> 2026-11-25 06:40Z."""
    s = str(ts or "")
    return f"{s[:10]} {s[11:16]}Z" if len(s) >= 16 and s[10] == "T" else s


def epoch(ts) -> float:
    try:
        import calendar
        return float(calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return 0.0


def fmt_secs(s) -> str:
    s = int(float(s or 0))
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h{(s % 3600) // 60:02d}m"


def ago(ts) -> str:
    e = epoch(ts)
    if not e:
        return "?"
    d = max(0, time.time() - e)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit}"
    return f"{int(d)}s"


def clip(s, n: int = 300) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def run(cmd, timeout: float = 10, input: Optional[str] = None, env=None) -> Tuple[int, str, str]:
    """A local command: (exit code, stdout, stderr). Never hands the child our stdin (the protocol pipe)."""
    try:
        r = subprocess.run(cmd, input=input, stdin=None if input is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out, f"timed out after {timeout:g}s"
    except OSError as e:
        return 127, "", str(e)


# ---------------------------------------------------------------- the table (ptrm.md)
HEAD = ["SSH_HOST", "LOCAL_PORT", "REMOTE_IP", "REMOTE_PORT", "NOTES"]


@dataclass
class Entry:
    """A row. Empty cells: SSH_HOST → ptrm chooses a host (auto_host; the choice lives in memory, the cell stays
    empty), REMOTE_IP → 127.0.0.1, REMOTE_PORT → LOCAL_PORT (or the port the chosen host serves it on).
    disabled (LOCAL_PORT written ~~8090~~, or 8090 disabled): ptrm leaves the row alone — no bring-up, no healing,
    no incident, no teach; the user can still open it by hand."""
    host: str
    lport: int
    rip: str = "127.0.0.1"
    rport: int = 0
    notes: str = ""
    auto_host: bool = False
    auto_rip: bool = False
    auto_rport: bool = False
    disabled: bool = False

    def __post_init__(self):
        self.rport = int(self.rport or self.lport)
        self.rip = self.rip or "127.0.0.1"

    @property
    def service(self) -> str:
        return self.notes.split()[0] if self.notes.strip() else ""

    @property
    def kind(self) -> str:
        s = self.service
        return s.split("/", 1)[0].lower() if "/" in s else ""

    @property
    def remote(self) -> str:
        return f"{self.rip}:{self.rport}"

    def cells(self) -> List[str]:
        """As written in the table: the cells left empty stay empty."""
        return ["" if self.auto_host else self.host, f"~~{self.lport}~~" if self.disabled else str(self.lport),
                "" if self.auto_rip else self.rip, "" if self.auto_rport else str(self.rport), self.notes]

    def to_dict(self) -> dict:
        return {"host": self.host, "port": self.lport, "remote_ip": self.rip, "remote_port": self.rport,
                "notes": self.notes, "service": self.service, "auto": self.auto_host, "disabled": self.disabled}


def default_table(hostname: Optional[str] = None) -> str:
    """As VimPage_ptrm: <pages>/<hostname>.ptrm.md when it exists, else <pages>/ptrm.md."""
    hostname = hostname or socket.gethostname()
    own = resolve_path(f"{PAGES}/{hostname}.ptrm.md")
    return own if os.path.isfile(own) else resolve_path(f"{PAGES}/ptrm.md")


PORT_CELL = re.compile(r"(?i)^(~~)?\s*[~#!-]?\s*(\d{2,5})\s*(~~)?\s*(disabled|disable|off)?\s*$")


def port_cell(cell: str) -> Tuple[Optional[int], bool]:
    """LOCAL_PORT cell → (port, disabled). Disabled: ~~8090~~ (markdown strikethrough, what ptrm writes), ~8090,
    #8090, -8090, !8090, 8090 disabled, 8090 off."""
    m = PORT_CELL.match((cell or "").strip())
    if not m:
        return None, False
    disabled = bool(m.group(1) or m.group(4) or re.match(r"\s*[~#!-]", cell or ""))
    return int(m.group(2)), disabled


def _cells(line: str) -> Optional[List[str]]:
    s = line.strip()
    if not s.startswith("|"):
        return None
    parts = s.split("|")
    return [p.strip() for p in parts[1:-1]] if len(parts) > 2 else None  # text after the last | (✅ ❌) is dropped


class Table:
    """The user's ptrm.md: the rows of its first table whose header starts with SSH_HOST."""

    def __init__(self, path: str, resolve=None):
        """resolve(lport) -> {"host", "rport"} | None: ptrm's choice for a row whose SSH_HOST is empty."""
        self.path = resolve_path(path)
        self.lock = threading.RLock()
        self.resolve = resolve

    def _lines(self) -> List[str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().split("\n")
        except OSError:
            return []

    def _block(self, lines) -> Tuple[int, int]:
        """[start, end) of the table block (contiguous | lines) holding the SSH_HOST header; (-1, -1) if none."""
        i = 0
        while i < len(lines):
            if lines[i].lstrip().startswith("|"):
                j = i
                while j < len(lines) and lines[j].lstrip().startswith("|"):
                    j += 1
                c = _cells(lines[i]) or []
                if c and c[0].upper() == "SSH_HOST":
                    return i, j
                i = j
            else:
                i += 1
        return -1, -1

    def entries(self) -> List[Entry]:
        with self.lock:
            lines = self._lines()
        a, b = self._block(lines)
        out = []
        for line in (lines[a:b] if a >= 0 else []):
            c = _cells(line)
            lport, disabled = port_cell(c[1]) if c and len(c) >= 2 else (None, False)
            if not lport:
                continue
            c += [""] * (5 - len(c))
            e = Entry(c[0], lport, c[2] or "127.0.0.1", int(c[3]) if c[3].isdigit() else lport,
                      " ".join(c[4:]).strip(), auto_host=not c[0], auto_rip=not c[2], auto_rport=not c[3].isdigit(),
                      disabled=disabled)
            if e.auto_host and self.resolve:
                a = self.resolve(e.lport) or {}
                e.host = a.get("host") or ""
                if e.auto_rport and a.get("rport"):
                    e.rport = int(a["rport"])
            out.append(e)
        return out

    def get(self, lport: int) -> Optional[Entry]:
        return next((e for e in self.entries() if e.lport == int(lport)), None)

    @staticmethod
    def render(entries: List[Entry]) -> List[str]:
        rows = [HEAD] + [e.cells() for e in entries]
        w = [max(len(r[i]) for r in rows) for i in range(5)]
        fmt = lambda r: "| " + " | ".join(r[i].ljust(w[i]) for i in range(5)) + " |"
        return [fmt(HEAD), "| " + " | ".join("-" * w[i] for i in range(5)) + " |"] + [fmt(r) for r in rows[1:]]

    def save(self, entries: List[Entry]):
        with self.lock:
            lines = self._lines()
            a, b = self._block(lines)
            block = self.render(entries)
            if a < 0:
                if not lines or lines == [""]:
                    lines = ["### PORT-REMOTE KM[ptrm]", "", "--------", ""]
                lines = lines + ([""] if lines[-1].strip() else []) + block + [""]
            else:
                lines = lines[:a] + block + lines[b:]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            os.replace(tmp, self.path)

    def upsert(self, e: Entry) -> Optional[Entry]:
        """Add the row, or replace the row with the same LOCAL_PORT. Returns the old row (None when new)."""
        with self.lock:
            rows = self.entries()
            old = next((x for x in rows if x.lport == e.lport), None)
            rows = [e if x.lport == e.lport else x for x in rows] if old else rows + [e]
            self.save(rows)
            return old

    def set_disabled(self, lport: int, on: bool) -> Optional[Entry]:
        """Mark the row disabled (LOCAL_PORT written ~~port~~) or enabled again. Returns the row (None: no such row)."""
        with self.lock:
            rows = self.entries()
            e = next((x for x in rows if x.lport == int(lport)), None)
            if e and e.disabled != bool(on):
                e.disabled = bool(on)
                self.save(rows)
            return e

    def remove(self, lport: int) -> Optional[Entry]:
        with self.lock:
            rows = self.entries()
            old = next((x for x in rows if x.lport == int(lport)), None)
            if old:
                self.save([x for x in rows if x.lport != int(lport)])
            return old


# ---------------------------------------------------------------- this machine: listeners, processes, ssh tunnels
def local_listeners() -> Dict[int, List[dict]]:
    """{port: [{pid, name, addr}]} of the TCP listeners on this machine (only your own processes without root)."""
    out: Dict[int, List[dict]] = {}

    def add(port, pid, name, addr):
        lst = out.setdefault(int(port), [])
        if not any(x["pid"] == pid and x["addr"] == addr for x in lst):
            lst.append({"pid": pid, "name": name, "addr": addr})

    if IS_WIN:
        rc, text, _ = run(["netstat", "-ano", "-p", "TCP"], 10)
        names = {}
        rc2, tl, _ = run(["tasklist", "/FO", "CSV", "/NH"], 10)
        for line in tl.splitlines():
            c = [x.strip('"') for x in line.split('","')]
            if len(c) >= 2 and c[1].strip('"').isdigit():
                names[int(c[1].strip('"'))] = c[0].strip('"')
        for line in text.splitlines():
            m = re.search(r"TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
            if m:
                pid = int(m.group(3))
                add(m.group(2), pid, names.get(pid, ""), m.group(1))
        return out
    if shutil.which("lsof"):
        rc, text, _ = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"], 10)
        pid, name = None, ""
        for line in text.splitlines():
            if line.startswith("p"):
                pid, name = int(line[1:]), ""
            elif line.startswith("c"):
                name = line[1:]
            elif line.startswith("n") and pid is not None:
                m = re.match(r"(.*):(\d+)$", line[1:])
                if m:
                    add(m.group(2), pid, name, m.group(1))
        return out
    rc, text, _ = run(["ss", "-ltnpH"], 10)
    for line in text.splitlines():
        m = re.search(r"\s(\S+):(\d+)\s", line)
        if m:
            p = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
            add(m.group(2), int(p.group(2)) if p else None, p.group(1) if p else "", m.group(1))
    return out


def proc_table() -> Dict[int, str]:
    """{pid: command line} of this machine's processes (empty on native Windows)."""
    if IS_WIN:
        return {}
    rc, text, _ = run(["ps", "-axo", "pid=,command="], 10)
    out = {}
    for line in text.splitlines():
        s = line.strip()
        pid, _, cmd = s.partition(" ")
        if pid.isdigit():
            out[int(pid)] = cmd.strip()
    return out


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        if IS_WIN:
            rc, text, _ = run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], 5)
            return str(int(pid)) in text
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")


def parse_ssh_argv(argv: List[str]) -> Tuple[Optional[str], List[str], List[str]]:
    """(destination, -L specs, -o options) of an ssh command line."""
    dest, fwd, opts, i = None, [], [], 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in SSH_ARG_OPTS:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = argv[i] if i < len(argv) else ""
                    if c == "L":
                        fwd.append(val)
                    elif c == "o":
                        opts.append(val)
                    break
                j += 1
            i += 1
            continue
        dest = a
        break
    return dest, fwd, opts


def parse_forward(spec: str) -> Optional[Tuple[str, int, str, int]]:
    """-L [bind:]lport:host:rport -> (bind, lport, rip, rport)."""
    parts = re.findall(r"\[[^\]]*\]|[^:]+", spec)
    parts = [p.strip("[]") for p in parts]
    try:
        if len(parts) == 3:
            return "", int(parts[0]), parts[1], int(parts[2])
        if len(parts) == 4:
            return parts[0], int(parts[1]), parts[2], int(parts[3])
    except ValueError:
        pass
    return None


def ssh_tunnels(procs: Optional[Dict[int, str]] = None) -> List[dict]:
    """The ssh -L forwards running on this machine: [{pid, host, lport, rip, rport, cmd}] (one per -L)."""
    out = []
    for pid, cmd in (procs if procs is not None else proc_table()).items():
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = cmd.split()
        if not argv or os.path.basename(argv[0]).lower() not in ("ssh", "ssh.exe"):
            continue
        dest, fwd, _ = parse_ssh_argv(argv)
        for spec in fwd:
            f = parse_forward(spec)
            if f and dest:
                out.append({"pid": pid, "host": dest, "lport": f[1], "rip": f[2], "rport": f[3], "cmd": cmd})
    return out


# ---------------------------------------------------------------- probe
def _http(port: int, path: str, timeout: float, host="127.0.0.1") -> Tuple[str, Optional[int], bytes, float]:
    """(state, http code, body, connect ms) of one GET at host:port."""
    t0 = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        return "closed", None, b"", 0.0
    except socket.timeout:
        return "timeout", None, b"", timeout * 1000
    except OSError as e:
        return "error:" + str(e), None, b"", 0.0
    ms = (time.monotonic() - t0) * 1000
    data = b""
    try:
        s.settimeout(timeout)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost:{port}\r\nUser-Agent: ptrm\r\nAccept: */*\r\n"
                  "Connection: close\r\n\r\n".encode())
        while len(data) < 65536:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        if not data:
            return "open", None, b"", ms
    except OSError:
        if not data:
            return "dead", None, b"", ms
    finally:
        try:
            s.close()
        except OSError:
            pass
    if not data:
        return "dead", None, b"", ms
    m = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", data)
    if not m:
        return "ok", None, data[:80], ms
    body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    return "ok", int(m.group(1)), body, ms


def probe(port: int, kind: str = "", timeout: float = 2.5) -> dict:
    """What answers at localhost:<port>. state: closed | dead (connected, then dropped: a tunnel with nothing behind) |
    open (silent: not HTTP, or stalled) | ok | loading (HTTP 503) | error | timeout. llm/ rows: /health then the model."""
    t0 = time.monotonic()
    path = "/health" if kind == "llm" else "/"
    state, code, body, ms = _http(port, path, timeout)
    r = {"port": int(port), "state": state, "ms": round(ms) if ms else None, "http": code}
    if state.startswith("error:"):
        r["state"], r["detail"] = "error", state[6:]
    if state == "ok" and code is not None:
        if code == 503:
            r["state"] = "loading"
        elif code >= 500:
            r["state"] = "error"
            r["detail"] = clip(body.decode("utf-8", "replace"), 120)
        if kind == "llm" and code == 404:  # not llama.cpp: an OpenAI-compatible server without /health
            r["state"] = "ok"
    elif state == "ok" and body:
        r["banner"] = clip(body.decode("utf-8", "replace"), 60)
    if kind == "llm" and r["state"] == "ok":
        st, c2, b2, _ = _http(port, "/v1/models", timeout)
        if st == "ok" and c2 == 200:
            try:
                data = json.loads(b2.decode("utf-8", "replace").split("\r\n0\r\n")[0].strip() or "{}")
            except ValueError:  # chunked body: take the JSON object out of it
                mm = re.search(rb"\{.*\}", b2, re.S)
                try:
                    data = json.loads(mm.group(0)) if mm else {}
                except ValueError:
                    data = {}
            models = [d.get("id") for d in data.get("data") or [] if isinstance(d, dict)]
            if models:
                r["model"] = re.sub(r"\.gguf$", "", os.path.basename(str(models[0]).replace("\\", "/")))
    r["total_ms"] = round((time.monotonic() - t0) * 1000)
    return r


# ---------------------------------------------------------------- remote hosts (ssh, BatchMode)
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10"]
WINPREP = "export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'\n"


REMOTE_SH = """sh -c 'eval "$(cat)"'"""  # read the whole script, then run it: nothing it starts sees unread input


def ssh_run(host: str, script: str, timeout: float = 40, ssh: str = "ssh") -> dict:
    """Run a sh script on the host, fed on stdin. The remote shell reads all of it before running it: on Windows (MSYS),
    a native program (tasklist …) started while script text is still unread in the pipe can hang. {rc, out, err, ms}."""
    t0 = time.monotonic()
    rc, out, err = run([ssh, *SSH_OPTS, host, REMOTE_SH], timeout, input=script)
    err = "\n".join(l for l in err.splitlines() if not l.startswith(("Warning: Permanently added", "** ")))
    return {"rc": rc, "out": out.replace("\r", ""), "err": err.strip(), "ms": round((time.monotonic() - t0) * 1000)}


def ssh_error(err: str, rc: int) -> str:
    """The reason an ssh call failed, in plain words, with the raw message."""
    e = err or ""
    for pat, why in ((r"Could not resolve hostname", "the name does not resolve (DNS/mDNS: is the host on this network?)"),
                     (r"Connection timed out|Operation timed out|timed out", "no answer (host down, asleep, or firewalled)"),
                     (r"Connection refused", "the host answers but no ssh server listens"),
                     (r"No route to host|Network is unreachable", "no route to the host (other network / VPN off?)"),
                     (r"Permission denied", "authentication refused (ptrm uses keys only: BatchMode; ssh-copy-id, or ptrmo for a password)"),
                     (r"Host key verification failed", "unknown or changed host key (ssh once by hand to accept it)"),
                     (r"bind \[?[^\]]*\]?:\s*Address already in use|Address already in use", "the local port is already taken"),
                     (r"connect failed|open failed", "the remote side refused the forwarded connection (service down there)")):
        if re.search(pat, e, re.I):
            return f"{why} — {clip(e, 200)}"
    if rc == 124:
        return "ssh did not finish in time — " + clip(e, 200)
    return clip(e, 240) or f"ssh exit {rc}"


def reach(host: str, ssh: str = "ssh") -> dict:
    r = ssh_run(host, "echo ptrm-ok; uname -s", 20, ssh)
    ok = r["rc"] == 0 and "ptrm-ok" in r["out"]
    return {"ok": ok, "ms": r["ms"], "os": r["out"].split("ptrm-ok", 1)[-1].strip() if ok else "",
            "err": "" if ok else ssh_error(r["err"], r["rc"])}


DISCOVER_SCRIPT = r"""
W=0; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) W=1; export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*';; esac
OS=$(uname -s 2>/dev/null)
echo "@@os"; uname -s; uname -r 2>/dev/null; uname -m; hostname
echo "@@cpu"
if [ $W = 1 ]; then powershell.exe -NoProfile -Command '(Get-CimInstance Win32_Processor).Name; $env:NUMBER_OF_PROCESSORS' 2>/dev/null
elif [ "$OS" = Darwin ]; then sysctl -n machdep.cpu.brand_string; sysctl -n hw.ncpu
else grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//'; nproc 2>/dev/null; fi
echo "@@mem"
if [ $W = 1 ]; then powershell.exe -NoProfile -Command '$o=Get-CimInstance Win32_OperatingSystem; [math]::Round($o.TotalVisibleMemorySize/1024); [math]::Round($o.FreePhysicalMemory/1024)' 2>/dev/null
elif [ "$OS" = Darwin ]; then echo $(( $(sysctl -n hw.memsize) / 1048576 )); vm_stat | awk '/page size of/{ps=$8} /Pages free|Pages inactive|Pages speculative/{gsub("\\.","",$NF); s+=$NF} END{if(!ps)ps=16384; print int(s*ps/1048576)}'
else awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print int(t/1024); print int(a/1024)}' /proc/meminfo; fi
echo "@@load"
if [ $W = 1 ]; then powershell.exe -NoProfile -Command '"cpu% " + (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average' 2>/dev/null
elif [ -r /proc/loadavg ]; then cut -d' ' -f1-3 /proc/loadavg; else sysctl -n vm.loadavg | tr -d '{}'; fi
echo "@@gpu"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null
elif [ $W = 1 ]; then powershell.exe -NoProfile -Command '(Get-CimInstance Win32_VideoController).Name' 2>/dev/null
elif [ "$OS" = Darwin ]; then system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/{print $2}'
else lspci 2>/dev/null | grep -Ei 'vga|3d controller|display' | cut -d: -f3- | sed 's/^ *//'; fi
echo "@@tools"
for t in htop btop nvtop macmon top nvidia-smi rocm-smi intel_gpu_top docker podman llama-server ollama vllm python3 python tmux curl watch; do p=$(command -v $t 2>/dev/null) && echo "$t $p"; done
echo "@@listen"
if [ $W = 1 ]; then netstat -ano 2>/dev/null | grep LISTENING | grep TCP
elif command -v ss >/dev/null 2>&1; then ss -ltnpH 2>/dev/null
elif command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null
else netstat -ltnp 2>/dev/null; fi
echo "@@tasks"
if [ $W = 1 ]; then tasklist /FO CSV /NH 2>/dev/null; fi
echo "@@procs"
if [ $W = 1 ]; then powershell.exe -NoProfile -Command 'Get-CimInstance Win32_Process | Where-Object { $_.Name -match "llama|ollama|vllm|python|node|java|docker|uvicorn|redis|postgres|mongo|nginx|comfy|jupyter" } | ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }' 2>/dev/null
else ps -eo pid=,args= 2>/dev/null | grep -Ei 'llama|ollama|vllm|python|node|java|docker|uvicorn|redis|postgres|mongo|nginx|comfy|jupyter' | grep -v -e grep -e 'sh -s' | sed 's/^ *//; s/ /\t/' | head -60; fi
echo "@@docker"
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null | head -30
echo "@@end"
"""

NOISE = {"system", "svchost.exe", "lsass.exe", "services.exe", "wininit.exe", "spoolsv.exe", "launchd", "rapportd",
         "controlce", "controlcenter", "sharingd", "systemd", "rpcbind", "cupsd", "avahi-daemon", "systemd-resolve",
         "systemd-resolved", "chronyd", "dnsmasq", "jetbrains-toolbox"}


def service_guess(name: str, cmd: str, port: int) -> Tuple[str, Optional[dict]]:
    """(service name, launch recipe or None) of a process listening on <port>."""
    low = (name + " " + cmd).lower()

    def arg(*flags):
        try:
            argv = shlex.split(cmd, posix=not re.match(r"^[a-zA-Z]:\\", cmd))
        except ValueError:
            argv = cmd.split()
        for i, a in enumerate(argv):
            for f in flags:
                if a == f and i + 1 < len(argv):
                    return argv[i + 1].strip('"')
                if a.startswith(f + "="):
                    return a.split("=", 1)[1].strip('"')
        return ""
    if "llama-server" in low or "llama_server" in low:
        model = arg("-m", "--model") or arg("-hf", "--hf-repo") or "?"
        model = re.sub(r"\.gguf$", "", os.path.basename(model.replace("\\", "/")))
        return f"llm/{model}", {"launch": cmd, "health": "/health"} if cmd else None
    if "ollama" in low:
        return "llm/ollama", ({"launch": cmd, "health": "/"} if cmd and " serve" in cmd else None)
    if "vllm" in low:
        m = re.search(r"vllm\S*\s+serve\s+(\S+)", cmd)
        return f"llm/vllm-{os.path.basename(m.group(1)) if m else '?'}", ({"launch": cmd, "health": "/health"} if cmd else None)
    if "http.server" in low:
        return "http/static", ({"launch": cmd, "health": "/"} if cmd else None)
    if "jupyter" in low:
        return "jupyter", None
    if "comfy" in low:
        return "comfyui", ({"launch": cmd, "health": "/"} if cmd else None)
    if port == 22:
        return "ssh", None
    if port == 3389:
        return "rdp", None
    if port == 5900:
        return "vnc", None
    if port == 2375 or port == 2376:
        return "docker-api", None
    base = os.path.basename((cmd.split()[0] if cmd else name).replace("\\", "/")).lower()
    return (re.sub(r"\.exe$", "", base) if base else ""), None


def sections(text: str) -> Dict[str, List[str]]:
    sec: Dict[str, List[str]] = {}
    cur = None
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(line.rstrip())
    return sec


def parse_discovery(text: str) -> dict:
    sec = sections(text)
    g = lambda k: [l for l in sec.get(k, []) if l.strip()]
    d: dict = {"complete": "end" in sec}
    o = g("os")
    d["os"] = {"system": o[0] if o else "", "release": o[1] if len(o) > 3 else "", "arch": o[-2] if len(o) >= 3 else "",
               "hostname": o[-1] if o else ""}
    d["windows"] = bool(re.match(r"MINGW|MSYS|CYGWIN", d["os"]["system"]))
    c = g("cpu")
    d["cpu"] = {"model": c[0].strip() if c else "", "count": int(c[-1]) if c and c[-1].strip().isdigit() else None}
    m = [x for x in g("mem") if x.strip().isdigit()]
    d["mem_mb"] = {"total": int(m[0]) if m else None, "free": int(m[1]) if len(m) > 1 else None}
    d["load"] = (g("load") or [""])[0].strip()
    d["gpu"] = [x.strip() for x in g("gpu")][:6]
    d["tools"] = {}
    for l in g("tools"):
        k, _, p = l.partition(" ")
        d["tools"][k] = p.strip()
    tasks = {}
    for l in g("tasks"):
        cells = [x.strip().strip('"') for x in l.split('","')]
        if len(cells) >= 2 and cells[1].isdigit():
            tasks[int(cells[1])] = cells[0].strip('"')
    procs = {}
    for l in g("procs"):
        pid, _, cmd = l.partition("\t")
        if pid.strip().isdigit():
            procs[int(pid)] = cmd.strip()
    listen: Dict[int, dict] = {}
    for l in g("listen"):
        if "LISTEN" not in l.upper() and not re.match(r"^\s*\S+\s+\d+\s+\d+\s", l):
            continue
        m = re.search(r"(?:^|\s)(\[[^\]]*\]|[\w\.\*%:-]*?):(\d+)(?=\s|$)", l)
        if not m:
            continue
        port = int(m.group(2))
        addr = m.group(1)
        pid, name = None, ""
        p = re.search(r'\(\("([^"]+)",pid=(\d+)', l)  # ss
        if p:
            name, pid = p.group(1), int(p.group(2))
        elif re.search(r"LISTENING\s+(\d+)\s*$", l):  # windows netstat
            pid = int(re.search(r"LISTENING\s+(\d+)\s*$", l).group(1))
        elif re.search(r"\s(\d+)/(\S+)\s*$", l):  # linux netstat -p
            mm = re.search(r"\s(\d+)/(\S+)\s*$", l)
            pid, name = int(mm.group(1)), mm.group(2)
        elif re.match(r"^(\S+)\s+(\d+)\s", l) and "(LISTEN)" in l:  # lsof
            mm = re.match(r"^(\S+)\s+(\d+)\s", l)
            name, pid = mm.group(1), int(mm.group(2))
        name = name or tasks.get(pid or -1, "")
        e = listen.setdefault(port, {"port": port, "addrs": [], "pid": pid, "name": name})
        if addr not in e["addrs"]:
            e["addrs"].append(addr)
        if not e["pid"] and pid:
            e["pid"], e["name"] = pid, name
    services, recipes = [], []
    for port, e in sorted(listen.items()):
        cmd = procs.get(e["pid"] or -1, "")
        name = e["name"] or (os.path.basename(cmd.split()[0].replace("\\", "/")) if cmd else "")
        svc, recipe = service_guess(name, cmd, port)
        noise = (name.lower() in NOISE or (d["windows"] and not cmd and port >= 49152)) and not recipe
        if noise or (not svc and not cmd):
            continue
        local_only = all(a in ("127.0.0.1", "[::1]", "::1", "localhost") for a in e["addrs"])
        services.append({"port": port, "service": svc, "pid": e["pid"], "process": name, "bind": ",".join(e["addrs"]),
                         "local_only": local_only, "cmd": clip(cmd, 400) if cmd else ""})
        if recipe:
            recipes.append(dict(service=svc, port=port, **recipe))
    d["listening"] = sorted(listen)
    d["services"] = services
    d["recipes"] = recipes
    dk = []
    for l in g("docker"):
        c = l.split("\t")
        if len(c) >= 2:
            dk.append({"name": c[0], "image": c[1], "ports": c[2] if len(c) > 2 else ""})
    d["docker"] = dk
    return d


def discover(host: str, ssh: str = "ssh", inventory: bool = True) -> dict:
    """What runs on the host (services, recipes, tools) and, with inventory, the host itself (ptrm_inv: hardware, OS,
    network, firewall, disks, power, sshd, account, keys) plus what this machine sees of it (access)."""
    script = DISCOVER_SCRIPT
    if inventory:
        import ptrm_inv
        script = script.replace('echo "@@end"', ptrm_inv.INV_SCRIPT + '\necho "@@end"')
    r = ssh_run(host, script, 90, ssh)
    if r["rc"] != 0 and "@@os" not in r["out"]:
        return {"ok": False, "ms": r["ms"], "err": ssh_error(r["err"], r["rc"])}
    d = parse_discovery(r["out"])
    d.update(ok=True, ms=r["ms"], at=utc_now())
    if inventory:
        import ptrm_inv
        inv = ptrm_inv.parse_inventory(sections(r["out"]), d["windows"], d["os"].get("system", ""))
        if inv.get("complete"):
            d["inv"] = inv
            d["access"] = ptrm_inv.access(host, inv, ssh)
    return d


def loopback_guard(command: str) -> Tuple[str, str]:
    """Services ptrm launches are reached through the tunnel: bind the known servers to 127.0.0.1 when the command
    names no address. (command, note about what was added or why it stays exposed)."""
    c = command
    if re.search(r"-m\s+http\.server\b", c) and not re.search(r"\s(--bind|-b)[\s=]", c):
        return c + " --bind 127.0.0.1", "added --bind 127.0.0.1"
    if re.search(r"llama-server|vllm|uvicorn|jupyter", c, re.I):
        m = re.search(r"--host[\s=](\S+)", c)
        if m and m.group(1).strip('"') not in ("127.0.0.1", "localhost"):
            return c, f"listens on {m.group(1)} (all interfaces), as the command asks"
        if not m and re.search(r"vllm|uvicorn|jupyter", c, re.I):
            return c + " --host 127.0.0.1", "added --host 127.0.0.1"
    return c, ""


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def launch(host: str, command: str, port: int, windows: bool, ssh: str = "ssh") -> dict:
    """Start <command> on the host, detached from the ssh session (it keeps running after ptrm leaves).
    POSIX: nohup in the background, output in ~/.ptrm/<port>.log. Windows (MSYS sshd): created through WMI
    (Win32_Process.Create), outside the session's job object, output in %USERPROFILE%\\.ptrm\\<port>.log.
    {ok, pid, log, err}."""
    if windows:
        command = resolve_windows_exe(host, command, ssh)
        inner = f'cmd.exe /c "{command} > "%USERPROFILE%\\.ptrm\\{int(port)}.log" 2>&1"'
        ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="
              + _ps_quote(inner) + "; CurrentDirectory=$env:USERPROFILE}; 'PID=' + $r.ProcessId + ' RC=' + $r.ReturnValue")
        script = WINPREP + "mkdir -p ~/.ptrm\npowershell.exe -NoProfile -Command " + shlex.quote(ps) + "\n"
        log = f"~/.ptrm/{int(port)}.log"
    else:
        log = f"~/.ptrm/{int(port)}.log"
        script = (f"mkdir -p ~/.ptrm; nohup sh -c {shlex.quote(command)} > {log} 2>&1 < /dev/null &\n"
                  "echo PID=$!\n")
    r = ssh_run(host, script, 40, ssh)
    m = re.search(r"PID=(\d+)", r["out"])
    rc = re.search(r"RC=(\d+)", r["out"])
    if r["rc"] != 0 or not m or (rc and rc.group(1) != "0") or m.group(1) == "0":
        return {"ok": False, "err": ssh_error(r["err"], r["rc"]) if r["rc"] else clip(r["out"] + " " + r["err"], 300),
                "log": log}
    return {"ok": True, "pid": int(m.group(1)), "log": log}


def stop(host: str, pid: int, windows: bool, ssh: str = "ssh") -> dict:
    pid = int(pid)
    if windows:
        script = WINPREP + f"taskkill /PID {pid} /T /F 2>&1\n"
    else:
        script = (f"kill {pid} 2>&1 && for i in 1 2 3 4 5 6; do kill -0 {pid} 2>/dev/null || {{ echo stopped; exit 0; }}; "
                  f"sleep 0.5; done; kill -9 {pid} 2>&1 && echo killed\n")
    r = ssh_run(host, script, 30, ssh)
    out = (r["out"] + " " + r["err"]).strip()
    ok = r["rc"] == 0 and not re.search(r"No such process|not found|ERROR", out, re.I)
    return {"ok": ok, "out": clip(out, 300)}


def health_path(h) -> str:
    """A health check as a path: "/health", "health", "http://127.0.0.1:8080/health" -> "/health"."""
    h = str(h or "/").strip()
    m = re.match(r"^[a-z]+://[^/]*(/.*)?$", h, re.I)
    if m:
        h = m.group(1) or "/"
    return h if h.startswith("/") else "/" + h


def remote_health(host: str, port: int, path: str = "/", ssh: str = "ssh", pid: Optional[int] = None,
                  windows: bool = False) -> dict:
    """HTTP code of http://127.0.0.1:<port><path> as seen from the host (000 = nothing answers); with pid, whether
    that process still runs (alive)."""
    script = f"curl -s -m 4 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{int(port)}{health_path(path)}; echo\n"
    if pid:
        script = (WINPREP + script + f"tasklist /FI \"PID eq {int(pid)}\" /NH | grep -q ' {int(pid)} ' && echo ALIVE || echo GONE\n"
                  if windows else script + f"kill -0 {int(pid)} 2>/dev/null && echo ALIVE || echo GONE\n")
    r = ssh_run(host, script, 20, ssh)
    lines = [l.strip() for l in r["out"].strip().splitlines() if l.strip()]
    code = next((l for l in lines if l.isdigit() and len(l) == 3), "000")
    out = {"ok": r["rc"] in (0, 1, 7, 28) and bool(lines), "code": code,
           "err": "" if r["rc"] in (0, 1, 7, 28) else ssh_error(r["err"], r["rc"])}
    if pid:
        out["alive"] = "ALIVE" in lines
    return out


def resolve_windows_exe(host: str, command: str, ssh: str = "ssh") -> str:
    """On a Windows (MSYS) host, a bare program name (python3, llama-server) as a Windows path: processes created
    through WMI do not get the MSYS PATH."""
    try:
        argv0 = shlex.split(command, posix=False)[0]
    except (ValueError, IndexError):
        return command
    if re.match(r'^"?([a-zA-Z]:[\\/]|\\\\)', argv0) or "/" in argv0 or "\\" in argv0:
        return command
    r = ssh_run(host, f'p=$(command -v {shlex.quote(argv0)}) && cygpath -w "$p"\n', 20, ssh)
    win = r["out"].strip().splitlines()[-1].strip() if r["rc"] == 0 and r["out"].strip() else ""
    if not win:
        return command
    return (f'"{win}"' if " " in win else win) + command[len(argv0):]


def proc_cmdline(host: str, pid: int, windows: bool, ssh: str = "ssh") -> str:
    """The command line of a process on the host (any process: discovery only reads the likely server ones)."""
    if windows:
        script = WINPREP + ("powershell.exe -NoProfile -Command "
                            + shlex.quote(f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine") + " < /dev/null\n")
    else:
        script = f"ps -o args= -p {int(pid)} 2>/dev/null\n"
    r = ssh_run(host, script, 25, ssh)
    return r["out"].strip().splitlines()[-1].strip() if r["rc"] == 0 and r["out"].strip() else ""


def remote_log(host: str, port: int, lines: int = 30, ssh: str = "ssh") -> str:
    """The tail of ~/.ptrm/<port>.log on the host (the output of a service ptrm launched there)."""
    r = ssh_run(host, f"f=~/.ptrm/{int(port)}.log; if [ ! -f $f ]; then echo '(no log: ptrm did not launch a service on {int(port)} there)'; "
                      f"elif [ ! -s $f ]; then echo '(the log is empty: the service wrote nothing yet)'; else tail -n {int(lines)} $f; fi 2>&1\n", 20, ssh)
    return r["out"].strip() or r["err"]


# ---------------------------------------------------------------- tunnels
class Tunnels:
    """ssh -N -L tunnels. Those ptrm opens are detached (own session) and listed in <run>/tunnels.json with
    want=true while they should stay open; scan() also adopts the ones it finds in ps (opened elsewhere)."""

    def __init__(self, run_dir: str, ssh: str = "ssh"):
        self.dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.path = os.path.join(run_dir, "tunnels.json")
        self.ssh = ssh
        self.lock = threading.RLock()
        self.children: Dict[int, subprocess.Popen] = {}

    def registry(self) -> Dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, reg: Dict[str, dict]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def set_want(self, lport: int, want: bool, e: Optional["Entry"] = None):
        """Keep the port open (healing reopens it when it is lost) or not. With e: also a port with no tunnel yet."""
        with self.lock:
            reg = self.registry()
            if str(lport) in reg:
                reg[str(lport)]["want"] = want
                self._save(reg)
            elif want and e:
                reg[str(lport)] = {"host": e.host, "rip": e.rip, "rport": e.rport, "pid": None, "want": True}
                self._save(reg)

    def reap(self):
        for pid, p in list(self.children.items()):
            if p.poll() is not None:
                self.children.pop(pid, None)

    def scan(self, procs: Optional[Dict[int, str]] = None) -> Dict[int, dict]:
        """{lport: tunnel} of the live tunnels: {pid, host, rip, rport, managed, since, want}."""
        self.reap()
        reg = self.registry()
        out: Dict[int, dict] = {}
        for t in ssh_tunnels(procs):
            rec = reg.get(str(t["lport"])) or {}
            managed = rec.get("pid") == t["pid"]
            out[t["lport"]] = dict(t, managed=managed, since=rec.get("started") if managed else None,
                                   want=bool(rec.get("want")) if managed else False)
        if IS_WIN:  # no command lines from ps: trust the registry's live pids
            for k, rec in reg.items():
                if rec.get("pid") and pid_alive(rec["pid"]) and int(k) not in out:
                    out[int(k)] = {"pid": rec["pid"], "host": rec["host"], "lport": int(k), "rip": rec["rip"],
                                   "rport": rec["rport"], "managed": True, "since": rec.get("started"),
                                   "want": bool(rec.get("want")), "cmd": rec.get("cmd", "")}
        return out

    def log_tail(self, lport: int, n: int = 8) -> str:
        try:
            with open(os.path.join(self.dir, f"ssh-{int(lport)}.log"), encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f if l.strip() and not l.startswith("# ")]
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def open(self, host: str, lport: int, rip: str, rport: int, wait: float = 12) -> dict:
        """Open localhost:<lport> -> <rip>:<rport> through <host>. {ok, pid, ms, err}."""
        cmd = [self.ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-L", f"{int(lport)}:{rip}:{int(rport)}", host]
        try:  # something already listens there: do not spawn (and keep that tunnel's log)
            socket.create_connection(("127.0.0.1", int(lport)), timeout=0.5).close()
            return {"ok": False, "err": f"the local port {lport} is already taken (something listens on it)"}
        except OSError:
            pass
        logp = os.path.join(self.dir, f"ssh-{int(lport)}.log")
        kw = {}
        if IS_WIN:
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        t0 = time.monotonic()
        with self.lock:
            with open(logp, "w", encoding="utf-8") as logf:
                logf.write(f"# {utc_now()} {' '.join(cmd)}\n")
                logf.flush()
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, **kw)
                except OSError as e:
                    return {"ok": False, "err": f"cannot run ssh: {e}"}
            self.children[p.pid] = p
            ok_since = None
            while time.monotonic() - t0 < wait:
                if p.poll() is not None:
                    self.children.pop(p.pid, None)
                    tail = self.log_tail(lport, 6)
                    return {"ok": False, "err": ssh_error(tail, p.returncode), "log": tail,
                            "ms": round((time.monotonic() - t0) * 1000)}
                try:
                    socket.create_connection(("127.0.0.1", int(lport)), timeout=0.5).close()
                    ok_since = ok_since or time.monotonic()
                    if time.monotonic() - ok_since > 0.4:  # still alive a moment after the port opened
                        break
                except OSError:
                    ok_since = None
                time.sleep(0.15)
            else:
                if p.poll() is None:
                    p.terminate()
                return {"ok": False, "err": f"the forward did not come up within {wait:g}s"
                        + (f" — {self.log_tail(lport, 4)}" if self.log_tail(lport, 4) else ""),
                        "ms": round((time.monotonic() - t0) * 1000)}
            reg = self.registry()
            reg[str(int(lport))] = {"pid": p.pid, "host": host, "rip": rip, "rport": int(rport), "started": utc_now(),
                                   "want": True, "cmd": " ".join(cmd)}
            self._save(reg)
            return {"ok": True, "pid": p.pid, "ms": round((time.monotonic() - t0) * 1000)}

    def close(self, lport: int, pid: Optional[int] = None) -> dict:
        """Stop the tunnel on <lport> (its ssh), and stop wanting it."""
        with self.lock:
            reg = self.registry()
            rec = reg.get(str(int(lport))) or {}
            pid = pid or rec.get("pid")
            if rec:
                rec["want"] = False
                rec["pid"] = None
                rec["closed"] = utc_now()
                self._save(reg)
            if not pid or not pid_alive(pid):
                return {"ok": True, "detail": "no live ssh for it"}
            try:
                if IS_WIN:
                    run(["taskkill", "/PID", str(pid), "/F"], 10)
                else:
                    os.kill(int(pid), signal.SIGTERM)
            except OSError as e:
                return {"ok": False, "err": str(e)}
            for _ in range(30):
                self.reap()
                if not pid_alive(pid):
                    return {"ok": True, "pid": pid}
                time.sleep(0.1)
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, AttributeError):
                pass
            return {"ok": True, "pid": pid, "detail": "killed"}


# ---------------------------------------------------------------- NOTES → what to run
GENERIC = {"llm", "it", "gguf", "the", "a", "an", "of", "and", "for", "on", "at", "to", "service", "server", "model",
           "new", "my", "test", "port", "local", "remote", "exe", "bin", "usr", "c", "users", "m", "py", "python",
           "python3", "host", "via", "run", "start", "0", "1", "k"}
SIZE = re.compile(r"^(?:e?\d+(?:\.\d+)?b|\d+x\d+b|a\d+b)$")  # 12b, e2b, e4b, 26b, a4b, 8x7b: a model's size


def note_tokens(text: str) -> List[str]:
    """The tokens of NOTES (or of a recipe) that tell services apart: gemma, 4, 12b, qat, q4 …"""
    t = re.sub(r"\.(gguf|safetensors|bin)\b", " ", str(text or "").lower())
    toks = [x for x in re.split(r"[^a-z0-9.]+|(?<=[a-z])\.|\.(?=[a-z])", t) if x]
    out = []
    for x in toks:
        x = x.strip(".")
        if x and x not in GENERIC and x not in out:
            out.append(x)
    return out


def note_match(notes: str, text: str) -> Tuple[float, List[str]]:
    """How well a recipe/service/model (text) matches NOTES: the share of NOTES' tokens found in it, 0 when a model
    size in NOTES (12b, e2b …) is missing from it. (score, matched tokens)."""
    want = note_tokens(notes)
    if not want:
        return 0.0, []
    have = set(note_tokens(text))
    have |= {h for x in list(have) for h in re.split(r"[_-]", x) if h}
    hit = [w for w in want if w in have or any(w in h for h in have if len(w) >= 4)]
    for w in want:
        if SIZE.match(w) and w not in hit:
            return 0.0, hit
    return len(hit) / len(want), hit


def notes_clear(notes: str) -> bool:
    """Does NOTES say what is expected at the port (beyond a kind like llm/)?"""
    return bool(note_tokens(notes))


# ---------------------------------------------------------------- memory
def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._-]+", "_", str(s or "")).strip("_.")[:120] or "_"


README = """# ptrm memory

What ptrm (the port-remote agent, ~/echo-home/.vim/mimd/ptrm) knows about every service it ever put at a local port
and every host it ever reached. Written by ptrm; plain files, safe to read and grep. Every time is UTC (…Z).

One folder per client machine: `<client hostname>/` (e.g. `MBK-4C2F.local/`). Each ptrm writes only its own folder,
so the folders of many clients merge without conflicts (sync them, e.g. with the echo-home repo); every ptrm also
reads the others': what one client learnt of a host (inventory, recipes, procedures, runbook, notes) and its cases
serve all of them. In each client folder:

- `events.jsonl`      one JSON object per line, append-only: open, close, state (a port's state changed), heal,
                      reach, discover, host_change, launch, stop, remote (a command run there), table (a row
                      added/changed/removed), incident, hands (remote hands asked), runbook, note.
                      Fields: ts, kind, port, host, remote, service, ok, detail …
- `hosts/<host>.yaml` every host ever accessed (the SSH_HOST spelling): first/last seen, accesses, last error,
                      identity and hardware (os, cpu, memory, gpu), tools (htop, nvtop, docker, llama-server …),
                      the services found listening at the last discovery, launch recipes (the command line that
                      started a service there, learnt from a discovery or a launch), notes (causes, fixes).
                      inventory: hardware (GPUs + VRAM, also the ones once attached), OS + last boot, network
                      (interfaces, Wi-Fi/Ethernet, IPs, DHCP, route, DNS, network name/category), firewall (does it
                      let ssh in), disks, power (AC/battery, sleep after idle, lid), sleep/wake/power-loss events,
                      sshd, account, keys; access: how this machine reaches it (ssh target, resolved IPs, host key,
                      our keys, the network this machine was on); profiles: each distinct hardware · network · OS
                      configuration it was seen in (p1, p2 …: moved, other network, new hardware); changes: what
                      differed between two looks; risks; runbook: physical steps a human did that brought it back;
                      incidents: how often it failed, why.
                      procedures: how a human brought a service up there (the commands typed in a teach
                      terminal), with the recipe learnt from it.
- `hosts/<host>.inv.jsonl`  every inventory that differed from the previous one (full snapshot + the changes).
- `ports/<port>.yaml` every local port ever opened: its profiles — one per (host, remote, service) it was mapped to:
                      first/last, opens, fails, incidents and their causes, the fixes that worked, the steps that
                      brought it up (bringup) — and notes.
- `cases.jsonl`       one line per incident and per request that changed something: symptom, layer, cause, evidence,
                      what ptrm did, what the human did, the fix, the outcome, how long it took. Recall reads them to
                      handle a new or similar request the way that worked.
- `run/`              runtime, not memory: tunnels.json (the ssh tunnels ptrm opened and whether to keep them open),
                      ssh-<port>.log (each tunnel's ssh output), incidents.json (the open incidents), assign.json
                      (the host ptrm chose for each row whose SSH_HOST is empty), teach.json (open teach terminals).
"""


MEMORY_PARTS = ("events.jsonl", "cases.jsonl", "hosts", "ports", "run")
HOST_FACTS = ("os", "cpu", "mem_mb", "load", "gpu", "tools", "docker", "windows", "services", "discovered", "inventory",
              "profile", "profiles", "changes", "risks")


def merge_hosts(own: dict, others: List[dict]) -> dict:
    """One view of a host from several clients' records: the facts of the newest look (inventory, services …),
    the union of what was learnt (recipes, procedures, runbook, notes), this client's own access/reached_from (they
    describe this machine's way to the host)."""
    docs = [d for d in [own] + others if d]
    out = dict(own or {})
    newest = max(docs, key=lambda d: str(d.get("discovered") or ""))
    for k in HOST_FACTS:
        if newest.get(k) is not None:
            out[k] = newest[k]
    if newest is not own and newest.get("_client"):
        out["facts_from"] = newest["_client"]
    for k, f in (("first_seen", min), ("last_seen", max), ("last_ok", max)):
        v = [d[k] for d in docs if d.get(k)]
        if v:
            out[k] = f(v)
    errs = [d["last_error"] for d in docs if d.get("last_error")]
    if errs:
        out["last_error"] = max(errs, key=lambda e: e.get("at", ""))
    out["accesses"] = sum(int(d.get("accesses") or 0) for d in docs)

    def union(key, ident):
        seen, items = {}, []
        for d in docs:
            for x in d.get(key) or []:
                k = ident(x)
                if k in seen:
                    old = seen[k]
                    if str(x.get("seen") or x.get("last") or "") > str(old.get("seen") or old.get("last") or ""):
                        items[items.index(old)] = x
                        seen[k] = x
                    continue
                seen[k] = x
                items.append(x)
        if items:
            out[key] = items
    union("recipes", lambda r: (r.get("service"), int(r.get("port") or 0)))
    union("procedures", lambda r: (r.get("service"), int(r.get("port") or 0), tuple(r.get("steps") or [])))
    union("runbook", lambda r: str(r.get("step", "")).lower())
    union("recipe_fails", lambda r: (r.get("launch"), r.get("at")))
    union("notes", lambda n: (n.get("at"), n.get("text")))
    inc = [d.get("incidents") or {} for d in docs if d.get("incidents")]
    if inc:
        layers: dict = {}
        for i in inc:
            for k, v in (i.get("layers") or {}).items():
                layers[k] = layers.get(k, 0) + int(v)
        out["incidents"] = {"count": sum(int(i.get("count") or 0) for i in inc), "layers": layers,
                            "last": max((i.get("last") or {} for i in inc), key=lambda x: x.get("at", ""))}
    out["clients"] = sorted({d.get("_client") for d in others if d.get("_client")} | ({"this"} if own.get("first_seen") else set()))
    out["host"] = own.get("host") or newest.get("host")
    return out


class Memory:
    """ptrm's memory: <base>/<client>/ is this machine's (it writes only there); the other <base>/<client>/ folders
    are other machines' ptrm memories (synced in, e.g. through the echo-home repo): ptrm reads them too — hosts
    (inventory, recipes, procedures, runbooks, notes), cases — so what one client learnt, every client knows."""

    def __init__(self, root: str = DEFAULT_MEMORY, client: Optional[str] = None):
        self.base = resolve_path(root)
        self.client = slug(client or socket.gethostname())
        self.root = os.path.join(self.base, self.client)
        self.lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, dict]] = {}
        os.makedirs(self.base, exist_ok=True)
        self.legacy = False
        self.migrated = self._migrate()
        if self.legacy:  # an older ptrm still writes the flat memory: share it until that one stops
            self.root = self.base
        for d in ("hosts", "ports", "run"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        rd = os.path.join(self.base, "README.md")
        try:
            cur = open(rd, encoding="utf-8").read() if os.path.isfile(rd) else ""
        except OSError:
            cur = None
        if cur is not None and cur != README:  # ptrm's own description of its memory: kept current
            with open(rd, "w", encoding="utf-8") as f:
                f.write(README)
        self.events_path = os.path.join(self.root, "events.jsonl")
        self.cases_path = os.path.join(self.root, "cases.jsonl")

    def _migrate(self) -> List[str]:
        """A memory from before the per-client folders (<base>/events.jsonl, hosts/ …) moves into <base>/<client>/."""
        if any(os.path.exists(os.path.join(self.root, x)) for x in ("events.jsonl", "hosts")):
            return []
        if not any(os.path.exists(os.path.join(self.base, x)) for x in MEMORY_PARTS):
            return []
        if self._flat_in_use():
            self.legacy = True
            return []
        moved = []
        for x in MEMORY_PARTS:
            src = os.path.join(self.base, x)
            if os.path.exists(src):
                os.makedirs(self.root, exist_ok=True)
                os.replace(src, os.path.join(self.root, x))
                moved.append(x)
        return moved

    def _flat_in_use(self) -> bool:
        """Another ptrm.py (an older one, before the per-client folders) runs on this memory base."""
        for pid, cmd in proc_table().items():
            if pid == os.getpid() or not re.search(r"(?i)python[0-9.]*\s+(-\w+\s+)*\S*ptrm\.py(\s|$)", cmd):
                continue
            m = re.search(r"--memory[ =](\S+)", cmd)
            if resolve_path(m.group(1) if m else (os.environ.get("PTRM_HOME") or DEFAULT_MEMORY)) == self.base:
                return True
        return False

    def peers(self) -> List[str]:
        """The other clients' memory folders."""
        out = []
        try:
            for n in sorted(os.listdir(self.base)):
                d = os.path.join(self.base, n)
                if n != self.client and os.path.isdir(d) and os.path.isdir(os.path.join(d, "hosts")):
                    out.append(d)
        except OSError:
            pass
        return out

    @property
    def run_dir(self) -> str:
        return os.path.join(self.root, "run")

    # ----- yaml docs
    def _load(self, path: str, default: dict) -> dict:
        """A YAML doc (parsed once per change of the file; callers get their own copy)."""
        import copy
        import yaml
        try:
            mt = os.path.getmtime(path)
            hit = self._cache.get(path)
            if hit and hit[0] == mt:
                return copy.deepcopy(hit[1])
            with open(path, encoding="utf-8") as f:
                d = yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
            if not isinstance(d, dict):
                return dict(default)
            self._cache[path] = (mt, d)
            return copy.deepcopy(d)
        except (OSError, ValueError, yaml.YAMLError):
            return dict(default)

    def _dump(self, path: str, d: dict):
        import yaml
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True, width=200)
        os.replace(tmp, path)

    def host_path(self, host: str) -> str:
        return os.path.join(self.root, "hosts", slug(host) + ".yaml")

    def port_path(self, port: int) -> str:
        return os.path.join(self.root, "ports", f"{int(port)}.yaml")

    def _own_host(self, host: str) -> dict:
        """This client's own record of the host (what it writes)."""
        return self._load(self.host_path(host), {"host": host})

    def host(self, host: str) -> dict:
        """What every client knows of the host, merged (this client's record first)."""
        own = self._own_host(host)
        others = []
        for peer in self.peers():
            p = os.path.join(peer, "hosts", slug(host) + ".yaml")
            if os.path.isfile(p):
                d = self._load(p, {})
                d["_client"] = os.path.basename(peer)
                others.append(d)
        return merge_hosts(own, others) if others else own

    def save_host(self, host: str, d: dict):
        with self.lock:
            d["host"] = host
            self._dump(self.host_path(host), d)

    # ----- disabled hosts (Xh in DASH): this client's own decision, kept in its own record of the host
    def host_disabled(self, host: str) -> bool:
        return bool(host) and bool(self._own_host(host).get("disabled"))

    def disabled_hosts(self) -> List[str]:
        return [h for h in self.known_hosts() if self.host_disabled(h)]

    def set_host_disabled(self, host: str, on: bool, why: str = ""):
        with self.lock:
            d = self._own_host(host)
            if on:
                d["disabled"] = {"at": utc_now(), "why": why or "disabled by the user"}
            else:
                d.pop("disabled", None)
            self.save_host(host, d)
        self.event("disable_host" if on else "enable_host", host=host, detail=why or None)

    def port(self, port: int) -> dict:
        d = self._load(self.port_path(port), {"port": int(port)})
        if "mappings" in d and "profiles" not in d:
            d["profiles"] = d.pop("mappings")
        return d

    def save_port(self, port: int, d: dict):
        with self.lock:
            d["port"] = int(port)
            self._dump(self.port_path(port), d)

    def known_hosts(self) -> List[str]:
        out = []
        for root in [self.root] + self.peers():
            for fn in sorted(os.listdir(os.path.join(root, "hosts"))):
                if fn.endswith(".yaml"):
                    h = self._load(os.path.join(root, "hosts", fn), {}).get("host") or fn[:-5]
                    if h not in out:
                        out.append(h)
        return out

    # ----- rows whose SSH_HOST is empty: the host ptrm chose (run/assign.json)
    def _assign_path(self) -> str:
        return os.path.join(self.run_dir, "assign.json")

    def assignments(self) -> Dict[str, dict]:
        try:
            with open(self._assign_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def assignment(self, lport: int) -> Optional[dict]:
        return self.assignments().get(str(int(lport)))

    def assign(self, lport: int, host: Optional[str], rport: Optional[int] = None, why: str = ""):
        with self.lock:
            d = self.assignments()
            if host:
                d[str(int(lport))] = {"host": host, "rport": int(rport) if rport else None, "at": utc_now(), "why": clip(why, 200)}
            else:
                d.pop(str(int(lport)), None)
            tmp = self._assign_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self._assign_path())
        if host:
            self.event("assign", port=int(lport), host=host, detail=clip(why, 200))

    def known_ports(self) -> List[int]:
        return sorted(int(fn[:-5]) for fn in os.listdir(os.path.join(self.root, "ports"))
                      if fn.endswith(".yaml") and fn[:-5].isdigit())

    # ----- events
    def event(self, kind: str, **fields) -> dict:
        rec = {"ts": utc_now(), "kind": kind}
        rec.update({k: v for k, v in fields.items() if v is not None and v != ""})
        with self.lock:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def events(self, port=None, host=None, kinds=None, limit: Optional[int] = None, tail_bytes: int = 4_000_000) -> List[dict]:
        try:
            with open(self.events_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail_bytes))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        lines = data.split("\n")
        if size > tail_bytes:
            lines = lines[1:]
        out = []
        for l in lines:
            if not l.strip():
                continue
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if port is not None and str(e.get("port")) != str(port):
                continue
            if host is not None and e.get("host") != host:
                continue
            if kinds and e.get("kind") not in kinds:
                continue
            out.append(e)
        return out[-limit:] if limit else out

    # ----- what happened
    def touch_host(self, host: str, ok: bool, ms=None, err: str = "", via: str = ""):
        with self.lock:
            d = self._own_host(host)
            now = utc_now()
            d.setdefault("first_seen", now)
            d["last_seen"] = now
            d["accesses"] = int(d.get("accesses") or 0) + 1
            if ok:
                d["last_ok"] = now
                if ms is not None:
                    d["ssh_ms"] = ms
                try:  # the network this machine was on: when the host is gone later, "did I move?"
                    import ptrm_inv
                    ln = ptrm_inv.local_net()
                    d["reached_from"] = {"label": ln.get("label"), "sig": ln.get("sig"), "at": now}
                except Exception:
                    pass
            else:
                d["fails"] = int(d.get("fails") or 0) + 1
                d["last_error"] = {"at": now, "err": clip(err, 300), "via": via}
            self.save_host(host, d)

    def port_opened(self, e: Entry, ok: bool, err: str = "", how: str = "open"):
        with self.lock:
            d = self.port(e.lport)
            now = utc_now()
            m = self._pprof(d, e.host, e.remote, e.service)
            m["last"] = now
            if ok:
                m["opens"] = int(m.get("opens") or 0) + 1
                m["last_ok"] = now
                d.setdefault("first_opened", now)
                d["last_opened"] = now
                d["current"] = {"host": e.host, "remote": e.remote, "service": e.service}
            else:
                m["fails"] = int(m.get("fails") or 0) + 1
                m["last_error"] = clip(err, 200)
            self.save_port(e.lport, d)
            self.event(how, port=e.lport, host=e.host, remote=e.remote, service=e.service, ok=ok, detail=clip(err, 300))

    def discovered(self, host: str, d: dict):
        with self.lock:
            h = self._own_host(host)
            if not d.get("ok"):
                self.save_host(host, h)
                self.touch_host(host, False, err=d.get("err", ""), via="discover")
                self.event("discover", host=host, ok=False, detail=d.get("err"))
                return h
            for k in ("os", "cpu", "mem_mb", "load", "gpu", "tools", "docker", "windows"):
                h[k] = d.get(k)
            h["services"] = d.get("services") or []
            h["discovered"] = d.get("at") or utc_now()
            recipes = h.setdefault("recipes", [])
            for r in d.get("recipes") or []:
                self._recipe(recipes, r, "discovered")
            changes = self._inventory(host, h, d["inv"], d.get("access")) if d.get("inv") else []
            self.save_host(host, h)
            self.touch_host(host, True, via="discover")
            svcs = ", ".join(f"{s['port']} {s['service']}" for s in h["services"][:8])
            self.event("discover", host=host, ok=True, detail=f"{h['os'].get('system')} · services: {svcs or 'none'}"
                       + (f" · profile {h.get('profile')}" if h.get("profile") else ""))
            out = self.host(host)
            out["_new_changes"] = changes
            return out

    def _inventory(self, host: str, h: dict, inv: dict, acc: Optional[dict]) -> List[dict]:
        """Keep the new inventory: match or add the host's profile, diff it with the last one, assess the risks.
        The changes of this look ([{what, sev}])."""
        import ptrm_inv as I
        old = h.get("inventory") or {}
        changes = I.diff(old, inv) if old else []
        profiles = h.setdefault("profiles", [])
        now = utc_now()
        p = I.match_profile(profiles, inv)
        if p:
            if old and h.get("profile") != p.get("id"):
                changes.insert(0, {"what": f"back to profile {p['id']} ({p.get('label')}; last seen {when(p.get('last'))})", "sev": ""})
            p["last"] = now
            p["seen"] = int(p.get("seen") or 0) + 1
        else:
            p = dict({"id": f"p{len(profiles) + 1}", "label": I.profile_label(inv), "first": now, "last": now, "seen": 1}, **I.facets(inv))
            profiles.append(p)
            if old:
                changes.insert(0, {"what": f"new profile {p['id']}: {p['label']}", "sev": "!"})
        h["profile"] = p["id"]
        h["inventory"] = inv
        if acc:
            h["access"] = acc
        h["risks"] = I.risks(inv, acc)
        if changes:
            h["changes"] = (h.get("changes") or [])[-29:] + [{"at": now, "profile": p["id"], "items": changes}]
            self.event("host_change", host=host, detail="; ".join(("⚠ " if c["sev"] else "") + c["what"] for c in changes)[:500])
        if changes or not old:
            with open(os.path.join(self.root, "hosts", slug(host) + ".inv.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": now, "profile": p["id"], "changes": changes, "inventory": inv, "access": acc},
                                   ensure_ascii=False) + "\n")
        return changes

    @staticmethod
    def _recipe(recipes: list, r: dict, source: str):
        now = utc_now()
        old = next((x for x in recipes if x.get("service") == r.get("service") and int(x.get("port") or 0) == int(r.get("port") or 0)), None)
        if old:
            old.update({k: v for k, v in r.items() if v})
            old["seen"] = now
        else:
            recipes.append(dict(r, learned=now, seen=now, source=source))

    def add_procedure(self, host: str, service: str, port: int, steps: List[str], by: str = "human", launch: str = ""):
        """How a human brought a service up on the host (the commands typed in a teach terminal)."""
        with self.lock:
            h = self._own_host(host)
            pr = h.setdefault("procedures", [])
            now = utc_now()
            old = next((x for x in pr if x.get("service") == service and int(x.get("port") or 0) == int(port)
                        and x.get("steps") == steps), None)
            if old:
                old["used"] = int(old.get("used") or 1) + 1
                old["last"] = now
            else:
                pr.append({"service": service, "port": int(port), "steps": [clip(x, 300) for x in steps][-30:],
                           "launch": clip(launch, 600), "by": by, "learned": now, "last": now, "used": 1})
            h["procedures"] = pr[-20:]
            self.save_host(host, h)
            self.event("procedure", host=host, service=service, detail=f"{len(steps)} steps for {service} on {port}: "
                       + " ; ".join(clip(x, 60) for x in steps[-4:]))

    def recipe_failed(self, host: str, recipe: dict, err: str):
        """A recipe that did not start the service: not recalled again until it works again (a launch)."""
        with self.lock:
            h = self._own_host(host)
            h["recipe_fails"] = (h.get("recipe_fails") or [])[-19:] + [
                {"service": recipe.get("service"), "port": recipe.get("port"), "launch": recipe.get("launch"),
                 "at": utc_now(), "err": clip(err, 200)}]
            self.save_host(host, h)

    def find_recipes(self, notes: str, rport: Optional[int] = None, host: Optional[str] = None) -> List[dict]:
        """What NOTES recalls: the recipes (and taught procedures) of every known host (or of <host>), ranked —
        [{host, recipe, score, why}]. A recipe counts when NOTES matches its service/command line (a model size in
        NOTES must be in it), or, when NOTES says nothing specific, when it is for the same port."""
        out = []
        clear = notes_clear(notes)
        for h in ([host] if host else self.known_hosts()):
            d = self.host(h)
            serving = {int(x.get("port") or 0): x for x in d.get("services") or []}
            failed = {}
            for f in d.get("recipe_fails") or []:
                failed[f.get("launch")] = max(failed.get(f.get("launch"), ""), f.get("at", ""))
            for r in d.get("recipes") or []:
                if failed.get(r.get("launch")) and failed[r.get("launch")] > str(r.get("last_ok") or ""):
                    continue  # it failed since it last worked
                text = f"{r.get('service', '')} {r.get('launch', '')}"
                sc, hit = note_match(notes, text) if clear else (0.0, [])
                why = []
                if sc >= 0.5:
                    why.append("NOTES match " + ",".join(hit))
                same_port = rport and int(r.get("port") or 0) == int(rport)
                if same_port:
                    sc += 0.3 if clear and sc else (0.5 if not clear else 0.0)
                    why.append(f"same port {rport}")
                if sc < 0.5:
                    continue
                if r.get("source") in ("launch", "taught"):
                    sc += 0.1
                    why.append(f"{r['source']} {when(r.get('seen') or r.get('learned'))}")
                if int(r.get("port") or 0) in serving:
                    why.append(f"serving now on {r.get('port')}")
                out.append({"host": h, "recipe": r, "score": round(sc, 2), "why": ", ".join(why)})
            for pr in d.get("procedures") or []:
                sc, hit = note_match(notes, f"{pr.get('service', '')} {' '.join(pr.get('steps') or [])}") if clear else (0.0, [])
                if rport and int(pr.get("port") or 0) == int(rport):
                    sc += 0.5 if not clear else 0.3 if sc else 0.0
                if sc >= 0.5 and not any(o["host"] == h and o["recipe"].get("port") == pr.get("port") for o in out):
                    out.append({"host": h, "recipe": {"service": pr.get("service"), "port": pr.get("port"),
                                                      "launch": pr.get("launch") or "", "health": "/", "source": "taught"},
                                "steps": pr.get("steps"), "score": round(sc, 2), "why": f"taught by {pr.get('by')} {when(pr.get('learned'))}"})
        out.sort(key=lambda x: -x["score"])
        return out

    def add_recipe(self, host: str, service: str, port: int, launch: str, health: str = "/", source: str = "launch"):
        with self.lock:
            h = self._own_host(host)
            r = {"service": service, "port": int(port), "launch": launch, "health": health}
            if source in ("launch", "taught"):
                r["last_ok"] = utc_now()
            self._recipe(h.setdefault("recipes", []), r, source)
            self.save_host(host, h)

    # ----- service profiles (per local port and mapping), cases, runbooks
    @staticmethod
    def _pprof(d: dict, host: str, remote: str, service: str = "") -> dict:
        profs = d.setdefault("profiles", [])
        m = next((x for x in profs if x.get("host") == host and x.get("remote") == remote), None)
        if not m:
            m = {"host": host, "remote": remote, "service": service, "first": utc_now(), "opens": 0, "fails": 0}
            profs.append(m)
        if service:
            m["service"] = service
        return m

    def port_failed(self, port: int, host: str, remote: str, service: str, layer: str, cause: str):
        """An incident on this mapping: count its cause (by layer), keep the last one."""
        with self.lock:
            d = self.port(port)
            m = self._pprof(d, host, remote, service)
            m["incidents"] = int(m.get("incidents") or 0) + 1
            c = m.setdefault("causes", {})
            c[layer or "unknown"] = int(c.get(layer or "unknown") or 0) + 1
            m["last_fail"] = {"at": utc_now(), "layer": layer, "cause": clip(cause, 200)}
            self.save_port(port, d)

    def port_fixed(self, port: int, host: str, remote: str, service: str, fix: str, secs: Optional[float] = None):
        with self.lock:
            d = self.port(port)
            m = self._pprof(d, host, remote, service)
            f = m.setdefault("fixes", {})
            k = clip(fix, 90)
            f[k] = int(f.get(k) or 0) + 1
            m["last_fix"] = {"at": utc_now(), "fix": clip(fix, 200), "after_s": round(secs) if secs else None}
            self.save_port(port, d)

    def port_bringup(self, port: int, host: str, remote: str, service: str, steps: List[str], model: str = ""):
        """The steps that brought this mapping up (the last time it took more than opening the tunnel)."""
        with self.lock:
            d = self.port(port)
            m = self._pprof(d, host, remote, service)
            m["ups"] = int(m.get("ups") or 0) + 1
            m["bringup"] = [clip(x, 160) for x in steps][-8:]
            m["bringup_at"] = utc_now()
            if model:
                m["model"] = model
            self.save_port(port, d)

    def case(self, kind: str, **rec) -> dict:
        """One incident or request, for recall: symptom → layer/cause → what was done → fix → outcome."""
        out = {"ts": utc_now(), "kind": kind}
        out.update({k: v for k, v in rec.items() if v not in (None, "", [], {})})
        with self.lock:
            with open(self.cases_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
        return out

    def cases(self, host=None, port=None, limit: Optional[int] = None, peers: bool = True) -> List[dict]:
        """Incidents, requests, lessons — this client's and (peers) the other clients' (oldest first)."""
        out = []
        for root in [self.root] + (self.peers() if peers else []):
            try:
                with open(os.path.join(root, "cases.jsonl"), encoding="utf-8") as f:
                    for l in f:
                        try:
                            c = json.loads(l)
                        except ValueError:
                            continue
                        if host and c.get("host") != host:
                            continue
                        if port is not None and int(port) not in [int(x) for x in c.get("ports") or ([c["port"]] if c.get("port") else [])]:
                            continue
                        if root != self.root:
                            c["client"] = os.path.basename(root)
                        out.append(c)
            except OSError:
                continue
        out.sort(key=lambda c: c.get("ts", ""))
        return out[-limit:] if limit else out

    @staticmethod
    def case_line(c: dict) -> str:
        ports = ",".join(str(p) for p in c.get("ports") or ([c["port"]] if c.get("port") else []))
        bits = [when(c.get("ts")), c.get("kind", ""), ports, c.get("host", ""), c.get("service", "")]
        head = " ".join(str(b) for b in bits if b) + (f" [{c['client']}]" if c.get("client") else "")
        body = []
        if c.get("request"):
            body.append(f"asked: {clip(c['request'], 120)}")
        if c.get("symptom"):
            body.append(c["symptom"])
        if c.get("layer") or c.get("cause"):
            body.append(f"{c.get('layer', '')}: {clip(c.get('cause'), 160)}".strip(": "))
        if c.get("steps"):
            body.append("did: " + " → ".join(clip(x, 70) for x in c["steps"][-6:]))
        if c.get("human"):
            body.append("human: " + " | ".join(clip(x, 90) for x in c["human"][-3:]))
        if c.get("fix"):
            body.append(f"fix: {clip(c['fix'], 140)}")
        if c.get("outcome"):
            body.append(c["outcome"] + (f" after {fmt_secs(c['secs'])}" if c.get("secs") else ""))
        return head + ": " + " · ".join(body)

    def runbook_add(self, host: str, step: str, src: str = "human") -> str:
        """A physical/manual step that mattered for this host (plug the eGPU, open the lid, log in after a reboot …):
        shown in every remote-hands checklist for it."""
        step = re.sub(r"\s+", " ", str(step or "")).strip()
        if not step:
            return "Error: an empty step"
        with self.lock:
            h = self._own_host(host)
            rb = h.setdefault("runbook", [])
            now = utc_now()
            old = next((x for x in rb if str(x.get("step", "")).lower() == step.lower()), None)
            if old:
                old["used"] = int(old.get("used") or 1) + 1
                old["last"] = now
            else:
                rb.append({"step": clip(step, 240), "learned": now, "last": now, "used": 1, "src": src})
            self.save_host(host, h)
            self.event("runbook", host=host, detail=step)
        return f"runbook of {host}: " + ("seen again" if old else "added") + f": {step}"

    def host_incident(self, host: str, layer: str, cause: str):
        with self.lock:
            h = self._own_host(host)
            i = h.setdefault("incidents", {})
            i["count"] = int(i.get("count") or 0) + 1
            ly = i.setdefault("layers", {})
            ly[layer or "unknown"] = int(ly.get(layer or "unknown") or 0) + 1
            i["last"] = {"at": utc_now(), "layer": layer, "cause": clip(cause, 200)}
            self.save_host(host, h)

    def note(self, target, text: str) -> str:
        with self.lock:
            t = str(target).strip()
            if t.isdigit():
                d = self.port(int(t))
                d.setdefault("notes", []).append({"at": utc_now(), "text": text})
                self.save_port(int(t), d)
                self.event("note", port=int(t), detail=text)
                return f"noted on port {t}"
            d = self._own_host(t)
            d.setdefault("notes", []).append({"at": utc_now(), "text": text})
            self.save_host(t, d)
            self.event("note", host=t, detail=text)
            return f"noted on host {t}"

    # ----- what is known, compact (for the LLM's small context)
    def host_line(self, host: str, d: Optional[dict] = None, full: bool = True) -> str:
        """What ptrm knows of a host, on one line (for the LLM's small context)."""
        d = d or self.host(host)
        if not d.get("first_seen"):
            return f"{host}: never reached" + (" · DISABLED by the user" if d.get("disabled") else "")
        bits = []
        if d.get("disabled"):
            bits.append(f"DISABLED by the user since {when(d['disabled'].get('at'))} (ptrm does not use it unless asked)")
        err = d.get("last_error") or {}
        rf = (d.get("reached_from") or {}).get("label")
        if d.get("last_ok") and (not err or err.get("at", "") <= d["last_ok"]):
            bits.append(f"✔ reached {when(d['last_ok'])}" + (f" ({d['ssh_ms']}ms)" if d.get("ssh_ms") else "")
                        + (f" from {rf}" if rf and full else ""))
        elif err:
            bits.append(f"✖ {when(err.get('at'))}: {clip(err.get('err'), 120)}" + (f" (last ok {when(d['last_ok'])} from {rf})" if d.get("last_ok") and rf else ""))
        inv = d.get("inventory")
        if inv:
            import ptrm_inv
            bits.append(ptrm_inv.summary(inv) if full else " ".join(f"{k} {v}" for k, v in ptrm_inv.chips(inv)))
            if d.get("profile"):
                p = next((x for x in d.get("profiles") or [] if x.get("id") == d["profile"]), {})
                bits.append(f"profile {d['profile']}/{len(d.get('profiles') or [])} since {when(p.get('first'))}")
        else:
            os_ = d.get("os") or {}
            if os_.get("system"):
                bits.append(f"{'Windows/MSYS' if d.get('windows') else os_.get('system')} {os_.get('arch', '')}".strip())
            cpu = d.get("cpu") or {}
            if cpu.get("model"):
                bits.append(f"{clip(cpu['model'], 40)} ×{cpu.get('count') or '?'}")
            mem = d.get("mem_mb") or {}
            if mem.get("total"):
                bits.append(f"{round(mem['total'] / 1024)}G RAM")
            if d.get("gpu"):
                bits.append("GPU " + " / ".join(clip(g, 50) for g in d["gpu"][:3]))
        if d.get("tools") and full:
            bits.append("tools " + " ".join(k for k in d["tools"] if k not in ("python", "curl", "top")))
        if d.get("services"):
            bits.append("serving " + ", ".join(f"{s['port']} {s['service']}" + ("" if s.get("local_only") else " (all interfaces)")
                                               for s in d["services"][:6]) + f" (at {when(d.get('discovered'))})")
        if d.get("recipes"):
            bits.append("recipes " + ", ".join(f"{r['service']}@{r['port']}" for r in d["recipes"][:5]))
        ch = (d.get("changes") or [])[-1:]
        if ch and time.time() - epoch(ch[0].get("at")) < 14 * 86400:
            bits.append(f"changed {when(ch[0]['at'])}: " + "; ".join(clip(x["what"], 80) for x in ch[0]["items"][:4]))
        rk = d.get("risks") or []
        if rk and full:
            bits.append("risks: " + "; ".join(("⚠ " if r.get("sev") else "") + clip(r["text"], 90) for r in sorted(rk, key=lambda r: not r.get("sev"))[:4]))
        if d.get("procedures"):
            bits.append("taught: " + " | ".join(f"{p.get('service')}@{p.get('port')}: " + " ; ".join(clip(x, 50) for x in (p.get("steps") or [])[-3:])
                                                  for p in d["procedures"][-2:]))
        if d.get("runbook"):
            bits.append("runbook: " + " | ".join(clip(r.get("step"), 90) for r in d["runbook"][-4:]))
        inc = d.get("incidents") or {}
        if inc.get("count"):
            last = inc.get("last") or {}
            bits.append(f"{inc['count']} incidents (" + ", ".join(f"{k}×{v}" for k, v in (inc.get("layers") or {}).items())
                        + f"; last {when(last.get('at'))} {clip(last.get('cause'), 90)})")
        if d.get("notes"):
            bits.append("notes: " + " | ".join(clip(n.get("text"), 100) for n in d["notes"][-3:]))
        if [c for c in d.get("clients") or [] if c != "this"] and full:
            bits.append("also known by " + ", ".join(c for c in d["clients"] if c != "this"))
        return f"{host}: " + " · ".join(bits)

    def port_line(self, port: int, d: Optional[dict] = None) -> str:
        d = d or self.port(port)
        profs = d.get("profiles") or []
        if not profs:
            return f"{port}: never opened"
        parts = []
        for m in sorted(profs, key=lambda m: m.get("last", ""), reverse=True)[:4]:
            x = (f"{m.get('host')} {m.get('remote')} {m.get('service', '')}" + (f" ▸{m['model']}" if m.get("model") else "")
                 + f" ({m.get('opens', 0)} opens, {m.get('fails', 0)} fails")
            if m.get("incidents"):
                x += f", {m['incidents']} incidents: " + ", ".join(f"{k}×{v}" for k, v in (m.get("causes") or {}).items())
            x += f", last {when(m.get('last'))})"
            if m.get("fixes"):
                x += " fixes: " + "; ".join(f"{k} ×{v}" for k, v in sorted(m["fixes"].items(), key=lambda kv: -kv[1])[:3])
            if m.get("bringup"):
                x += " · brought up by: " + " → ".join(clip(s, 60) for s in m["bringup"][-5:])
            parts.append(x)
        notes = " · notes: " + " | ".join(clip(n.get("text"), 100) for n in d["notes"][-3:]) if d.get("notes") else ""
        return f"{port}: " + "; ".join(parts) + notes

    @staticmethod
    def event_line(e: dict) -> str:
        bits = [when(e.get("ts")), e.get("kind", "")]
        if e.get("port") is not None:
            bits.append(str(e["port"]))
        if e.get("host"):
            bits.append(e["host"])
        if e.get("remote"):
            bits.append(e["remote"])
        if e.get("service"):
            bits.append(e["service"])
        if "ok" in e:
            bits.append("ok" if e["ok"] else "FAILED")
        if e.get("detail"):
            bits.append("— " + clip(e["detail"], 200))
        return " ".join(str(b) for b in bits if b != "")

    def history(self, port=None, host=None, limit: int = 25) -> str:
        head = self.port_line(int(port)) if port is not None else self.host_line(host) if host else ""
        cs = self.cases(host=host, port=port, limit=5)
        ev = self.events(port=port, host=host, limit=limit)
        return ((head + "\n" if head else "") + ("cases:\n" + "\n".join("  " + self.case_line(c) for c in cs) + "\nevents:\n" if cs else "")
                + ("\n".join(self.event_line(e) for e in ev) or "(no events)"))

    def recall(self, query: str, k: int = 8) -> List[Tuple[float, str]]:
        """BM25 over everything ptrm remembers: host and port summaries, notes, events (newest win ties)."""
        docs: List[str] = []
        for h in self.known_hosts():
            d = self.host(h)
            docs.append(self.host_line(h, d))
            docs += [f"note on {h} ({when(n.get('at'))}): {n.get('text')}" for n in d.get("notes") or []]
            docs += [f"recipe on {h}: {r.get('service')} port {r.get('port')}: {clip(r.get('launch'), 200)}"
                     for r in d.get("recipes") or []]
            docs += [f"runbook of {h} ({when(r.get('learned'))}, used {r.get('used', 1)}×): {r.get('step')}" for r in d.get("runbook") or []]
            docs += [f"procedure on {h} for {p.get('service')} port {p.get('port')} (taught by {p.get('by')} {when(p.get('learned'))}): "
                     + " ; ".join(p.get("steps") or []) for p in d.get("procedures") or []]
            docs += [f"risk on {h}: {r.get('text')} (fix: {r.get('fix')})" for r in d.get("risks") or []]
            docs += [f"profile {p.get('id')} of {h} ({when(p.get('first'))} – {when(p.get('last'))}): {p.get('label')}" for p in d.get("profiles") or []]
            docs += [f"{h} changed {when(c.get('at'))}: " + "; ".join(x.get("what", "") for x in c.get("items") or []) for c in d.get("changes") or []]
        for p in self.known_ports():
            d = self.port(p)
            docs.append("port " + self.port_line(p, d))
            docs += [f"note on port {p} ({when(n.get('at'))}): {n.get('text')}" for n in d.get("notes") or []]
        docs += ["case " + self.case_line(c) for c in self.cases(limit=600)]
        docs += [self.event_line(e) for e in self.events(limit=3000) if e.get("kind") != "state" or not e.get("ok")]
        tok = lambda s: re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*|[a-z0-9]+", s.lower())
        q = set(tok(query))
        q |= {p for t in list(q) for p in re.split(r"[._-]", t) if p}
        if not q:
            return []
        toks = [tok(d) for d in docs]
        for i, t in enumerate(toks):
            toks[i] = t + [p for x in t for p in re.split(r"[._-]", x) if p and p != x]
        n = len(docs)
        avg = sum(len(t) for t in toks) / max(1, n)
        df = {w: sum(1 for t in toks if w in t) for w in q}
        scored = []
        for i, t in enumerate(toks):
            s = 0.0
            for w in q:
                f = t.count(w)
                if f:
                    idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(t) / avg))
            if s > 0:
                scored.append((s + i * 1e-9, docs[i]))
        scored.sort(reverse=True)
        seen, out = set(), []
        for s, d in scored:
            if d not in seen:
                seen.add(d)
                out.append((round(s, 2), d))
            if len(out) >= k:
                break
        return out


# ---------------------------------------------------------------- status: the table, live
ICON = {"up": "✅", "loading": "⏳", "down": "🟠", "lost": "❌", "off": "⚪", "busy": "⛔", "local": "🏠", "other": "🔀",
        "stalled": "⌛", "error": "❗", "unlisted": "🔀", "opening": "⏳", "disabled": "🚫"}  # all double-width: the DASH columns stay aligned


def status(table: Table, tunnels: Tunnels, timeout: float = 2.5) -> Tuple[List[dict], Dict[int, List[dict]]]:
    """The table's rows with their live state, then the unlisted tunnels. Row state:
    up (answers) · loading (503) · down (a tunnel, nothing behind it: the service is down there) · stalled (connects,
    says nothing) · error (HTTP 5xx) · lost (ptrm wants it open and nothing listens) · off (not open) · busy (another
    local process holds the port) · local (a local process serves it) · other (a tunnel to another host/port) ·
    disabled (the row is disabled and nothing listens: ptrm leaves it alone)."""
    entries = table.entries()
    procs = proc_table()
    tun = tunnels.scan(procs)
    listeners = local_listeners()
    reg = tunnels.registry()
    with ThreadPoolExecutor(max_workers=8) as ex:
        probes = dict(zip([e.lport for e in entries], ex.map(lambda e: probe(e.lport, e.kind, timeout), entries)))
    rows = []
    for e in entries:
        t = tun.get(e.lport)
        adopt = False
        if e.auto_host and not e.host and t:  # no host chosen yet, a tunnel is open: it is the choice
            e.host, adopt = t["host"], True
            if e.auto_rport:
                e.rport = int(t["rport"])
        L = [x for x in listeners.get(e.lport, []) if not t or x["pid"] != t["pid"]]
        pr = probes[e.lport]
        rec = reg.get(str(e.lport)) or {}
        row = dict(e.to_dict(), probe=pr, tunnel=t, holders=L, want=bool(rec.get("want")), adopt=adopt)
        if t and (t["host"], str(t["rip"]), int(t["rport"])) != (e.host, e.rip, e.rport) \
                and not (t["host"] == e.host and int(t["rport"]) == e.rport and t["rip"] in ("localhost", "127.0.0.1") and e.rip in ("localhost", "127.0.0.1")):
            st = "other"
        elif t:
            st = {"ok": "up", "loading": "loading", "dead": "down", "open": "stalled", "error": "error",
                  "timeout": "stalled", "closed": "lost"}.get(pr["state"], "error")
        elif pr["state"] != "closed":
            st = "local" if L and pr["state"] in ("ok", "loading", "open") else "busy"
        else:
            st = "lost" if row["want"] else "off"
        if e.disabled and st in ("off", "lost"):
            st = "disabled"
        row["state"] = st
        rows.append(row)
    listed = {e.lport for e in entries}
    for lport, t in sorted(tun.items()):
        if lport not in listed:
            rows.append({"host": t["host"], "port": lport, "remote_ip": t["rip"], "remote_port": t["rport"], "notes": "",
                         "service": "", "probe": None, "tunnel": t, "holders": [], "want": t.get("want", False),
                         "state": "unlisted"})
    return rows, listeners


def row_line(r: dict, width_host: int = 28) -> str:
    """One compact line per row (for the LLM and the shell)."""
    pr = r.get("probe") or {}
    t = r.get("tunnel")
    host = (("~" + r["host"]) if r["host"] else "(ptrm chooses)") if r.get("auto") else r["host"]
    bits = [f"{r['port']:<5}", f"{ICON.get(r['state'], '?')} {r['state']:<8}", f"{host:<{width_host}}",
            f"{r['remote_ip']}:{r['remote_port']:<6}", r.get("notes") or "(no notes)"]
    extra = []
    if pr.get("model"):
        extra.append(f"model {pr['model']}")
    if pr.get("http"):
        extra.append(f"HTTP {pr['http']}")
    if pr.get("ms") is not None and r["state"] in ("up", "loading", "local", "stalled"):
        extra.append(f"{pr['ms']}ms")
    if t:
        extra.append(f"tunnel ssh pid {t['pid']}" + (" by ptrm" if t.get("managed") else " (opened elsewhere)")
                     + (f" → {t['host']} {t['rip']}:{t['rport']}" if r["state"] in ("other", "unlisted") else ""))
    if r.get("holders"):
        extra.append("held by " + ", ".join(f"{h['name']} pid {h['pid']}" for h in r["holders"][:3]))
    if r.get("want") and r["state"] in ("lost", "off"):
        extra.append("ptrm keeps it open")
    if r.get("disabled"):
        extra.append("disabled: ptrm leaves it alone")
    if r.get("host_off"):
        extra.append("its host is disabled: ptrm leaves it alone")
    return " ".join(bits) + (f"  [{', '.join(extra)}]" if extra else "")


def free_ports(listeners: Dict[int, List[dict]], start: int = 8081, n: int = 5, avoid=()) -> List[int]:
    out, p = [], start
    while len(out) < n and p < 65535:
        if p not in listeners and p not in avoid:
            try:
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                s.close()
                out.append(p)
            except OSError:
                pass
        p += 1
    return out


# ---------------------------------------------------------------- shell
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="ptrm_core.py", description="ptrm engine: the port-remote table, tunnels, probes, hosts, memory")
    ap.add_argument("--table", default="", help="ptrm.md (default: ~/echo-home/.vim/pages/<hostname>.ptrm.md, else ptrm.md)")
    ap.add_argument("--memory", default=os.environ.get("PTRM_HOME") or DEFAULT_MEMORY,
                    help="the memory base; this machine writes <base>/<hostname>/ and reads the other clients' folders")
    ap.add_argument("--ssh", default="ssh")
    ap.add_argument("cmd", choices=["status", "check", "open", "close", "tunnels", "reach", "discover", "hosts", "history",
                                    "recall", "note", "listen", "inventory", "diagnose", "profiles", "cases", "runbook"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    mem = Memory(a.memory)
    table = Table(a.table or default_table(), resolve=mem.assignment)
    tun = Tunnels(mem.run_dir, a.ssh)
    arg0 = a.args[0] if a.args else ""
    if a.cmd == "status":
        rows, _ = status(table, tun)
        print(f"# {table.path} · {utc_now()}")
        for r in rows:
            print(row_line(r))
    elif a.cmd == "check":
        e = table.get(int(arg0))
        print(json.dumps(probe(int(arg0), e.kind if e else ""), indent=1))
    elif a.cmd == "open":
        e = table.get(int(arg0))
        if not e:
            sys.exit(f"no row for port {arg0} in {table.path}")
        r = tun.open(e.host, e.lport, e.rip, e.rport)
        mem.port_opened(e, r["ok"], r.get("err", ""))
        mem.touch_host(e.host, r["ok"], err=r.get("err", ""), via="tunnel")
        print(json.dumps(r, indent=1))
    elif a.cmd == "close":
        r = tun.close(int(arg0))
        e = table.get(int(arg0))
        mem.event("close", port=int(arg0), host=e.host if e else None, ok=r["ok"], detail=r.get("err") or r.get("detail"))
        print(json.dumps(r, indent=1))
    elif a.cmd == "tunnels":
        for lp, t in sorted(tun.scan().items()):
            print(f"{lp} → {t['host']} {t['rip']}:{t['rport']} pid {t['pid']} {'ptrm' if t['managed'] else 'elsewhere'}")
    elif a.cmd == "reach":
        r = reach(arg0, a.ssh)
        mem.touch_host(arg0, r["ok"], r["ms"], r["err"], via="reach")
        print(json.dumps(r, indent=1))
    elif a.cmd in ("discover", "inventory"):
        d = discover(arg0, a.ssh)
        h = mem.discovered(arg0, d)
        print(mem.host_line(arg0))
        if a.cmd == "inventory" and h.get("inventory"):
            import ptrm_inv
            print(f"profile {h.get('profile')}: " + ptrm_inv.profile_label(h["inventory"]))
            for c in h.get("_new_changes") or []:
                print(("⚠ " if c["sev"] else "  ") + "changed: " + c["what"])
            for r in h.get("risks") or []:
                print(("⚠ " if r["sev"] else "  ") + "risk: " + r["text"] + (f"  (fix: {r['fix']})" if r.get("fix") else ""))
    elif a.cmd == "diagnose":
        import ptrm_inv
        dg = ptrm_inv.diagnose(arg0, mem.host(arg0), a.ssh, int(a.args[1]) if len(a.args) > 1 else None)
        print(f"{dg['layer']}: {dg['cause']}")
        for x in dg["evidence"]:
            print("  · " + x)
        for x in dg["auto"]:
            print("  ptrm can: " + x)
        for i, x in enumerate(dg["hands"], 1):
            print(f"  {i}. {x}")
    elif a.cmd == "profiles":
        h = mem.host(arg0)
        for p in h.get("profiles") or []:
            print(f"{'*' if p.get('id') == h.get('profile') else ' '} {p.get('id')} {when(p.get('first'))} – {when(p.get('last'))} "
                  f"seen {p.get('seen')}×: {p.get('label')}")
        for c in h.get("changes") or []:
            print(f"  {when(c.get('at'))} → {c.get('profile')}: " + "; ".join(x["what"] for x in c.get("items") or []))
    elif a.cmd == "cases":
        k = dict(port=int(arg0)) if arg0.isdigit() else dict(host=arg0) if arg0 else {}
        for c in mem.cases(limit=30, **k):
            print(mem.case_line(c))
    elif a.cmd == "runbook":
        if len(a.args) > 1:
            print(mem.runbook_add(arg0, " ".join(a.args[1:]), "shell"))
        for r in mem.host(arg0).get("runbook") or []:
            print(f"- {r.get('step')}  ({when(r.get('learned'))}, used {r.get('used')}×, {r.get('src')})")
    elif a.cmd == "hosts":
        for h in mem.known_hosts():
            print(mem.host_line(h))
    elif a.cmd == "history":
        print(mem.history(port=int(arg0)) if arg0.isdigit() else mem.history(host=arg0))
    elif a.cmd == "recall":
        for s, d in mem.recall(" ".join(a.args)):
            print(f"{s:5.2f} {d}")
    elif a.cmd == "note":
        print(mem.note(arg0, " ".join(a.args[1:])))
    elif a.cmd == "listen":
        for p, L in sorted(local_listeners().items()):
            print(p, ", ".join(f"{x['name']}({x['pid']}) {x['addr']}" for x in L))


if __name__ == "__main__":
    _main()
