#!/usr/bin/env python
"""
disp: the image agent of Vim. Shows images to the user on the disp pad (a Rust GUI window: zoom, crop, marks,
touching pixels, clipboard, the strip of every image ever loaded), looks at them through the local multimodal LLM
(llama.cpp serving a vision model: --mmproj) and talks with the user about them in Vim.
Headless backend of disp.lua (same folder).

The pad (pad/, binary pad/target/release/disppad, window DISPAD) is the medium between the user and the pixels. disp
drives it through its control socket (127.0.0.1:--pad-port, NDJSON) and hears what the user does there (loaded, focus,
touch, select, mark, view, ask, pick). The pad reads every image where it is (its path; never a copy) and hashes it on
each load: the sha256 names the image's folder in the memory both share, where the pad keeps the strip (catalog.jsonl),
the marks and the only copy of a pasted or cropped image.

disp's own memory is ~/echo-home/memory/gddr9b (disp_map.py): per content (sha256) the MAP of the image — a flat
inventory of what the eye recognises (text lines and words, buttons, boxes, icons, shapes, figures, colour regions,
patterns), turned into a DOM-like tree with text blocks and geometric / semantic relations — its notes (questions
answered, findings) and the TRACE of everything the user did with it on the pad. Reopening the same content reloads
all of it. Mapping is the prefill: done once in the background, so later questions read the map, not the pixels.

The reasoning loop is text only (the 16k context stays small); the "eye" is a separate multimodal call that gets the
pad's export (region, scale, marks and grid burnt in) and answers in text. New images get a short glance (caption).

Protocol with Neovim: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"w","b":"chat"|"iface"|"log","text":..,"hl":<highlight group>|null}   append lines
  {"t":"w","b":"log","text":..,"raw":true}   streamed LLM text (continues the stream's line)
  {"t":"w","b":"log","text":"","eos":true}   end of an LLM stream
  {"t":"state","busy":b,"phase":..,"queue":n,"auto":b,"approval":<text>|null,"net":{..},"model":..,
             "pad":{"connected":b,"count":n,"focus":{n,name,w,h,sha}|null},"glance":b,"map_auto":b,"mapping":<key>|null}
  {"t":"map","key":"<sha4>#<n>","n":..,"sha":..,"title":..,"lines":[..],"focus":b}   an image's MAP pane content
  {"t":"map_hit","key":..,"id":"e5"}   the user touched element e5 on the pad
  {"t":"req","id":N,"op":"context"|"ex"|"lua"|"insert", ...}   run in the user's editor
  {"t":"show"}   {"t":"exit"}
nvim -> backend
  {"t":"input","text":..,"ctx":{buffer,line,image}|null}
  {"t":"res","id":N,"ok":b,"out":..,"err":..}
  {"t":"open","path":..}  {"t":"paste"}  {"t":"focus","image":..}  {"t":"pad","op":"raise"|"quit"}
  {"t":"map","image":n}  (send me that image's MAP)   {"t":"map_go","image":n,"id":"e5","op":"show"|"mark"|"build"}
  {"t":"net","op":"join"|"leave"|"say", ...}
  {"t":"cancel"} | {"t":"quit"}
"""
import argparse
import base64
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
from typing import Optional

_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def resolve_path(path_str: str) -> str:
    """~ and Git Bash/MSYS paths (/c/Users/..) to native ones (C:/Users/..), as memphia_core.resolve_path."""
    if not path_str:
        return path_str
    if path_str.startswith("~"):
        path_str = os.path.join(os.environ.get("HOME", os.path.expanduser("~")), path_str[1:].lstrip("/\\"))
    if os.name == "nt" or os.environ.get("MSYSTEM"):
        m = re.match(r"^[\\/]([a-zA-Z])(?:[\\/]|$)(.*)", path_str)
        if m and not os.path.exists(path_str):
            path_str = f"{m.group(1).upper()}:/{m.group(2)}"
    if not re.match(r"^[a-zA-Z]:", path_str):
        path_str = os.path.abspath(path_str)
    return os.path.normpath(path_str).replace("\\", "/")


HERE = resolve_path(os.path.dirname(os.path.abspath(__file__)))
for _d in (os.path.dirname(os.path.abspath(__file__)), HERE):
    if _d not in sys.path:
        sys.path.insert(0, _d)
for _stream in (sys.stdin, sys.stderr):  # the protocol is UTF-8 whatever the Windows code page is
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ARGS = None
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico")

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
    """A line in the LLM pane (the raw LLM output)."""
    emit({"t": "w", "b": "log", "text": text, "hl": hl})


def clip(s, n: int = 300) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_llm_json(raw: str) -> Optional[dict]:
    """First balanced JSON object in the text (tolerates fences, prose, <think> blocks)."""
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


def truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- LLM (text and vision)
class LLM:
    def __init__(self, host, port):
        from openai import OpenAI
        self.base = f"http://{host}:{port}"
        self.client = OpenAI(base_url=self.base + "/v1", api_key="localm", timeout=600)
        self.model = None
        self.vision = None  # None = not checked yet

    def ensure(self) -> bool:
        if self.model:
            return True
        try:
            models = self.client.models.list()
            self.model = models.data[0].id if models.data else None
        except Exception:
            self.model = None
        return bool(self.model)

    def has_vision(self) -> bool:
        """llama-server /props: modalities.vision (the server needs --mmproj). Rechecked until it is true."""
        if self.vision:
            return True
        try:
            from urllib.request import urlopen
            with urlopen(self.base + "/props", timeout=5) as r:
                props = json.loads(r.read().decode("utf-8", "replace"))
            self.vision = bool((props.get("modalities") or {}).get("vision"))
        except Exception:
            self.vision = None
        return bool(self.vision)

    def stream(self, messages, cancel: threading.Event, max_tokens: Optional[int] = None, think: bool = True) -> str:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        kw = {"max_tokens": max_tokens} if max_tokens else {}
        if not think:  # gemma-4's template thinks unless told not to: the eye answers at once
            kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.0, stream=True, **kw)
        out, buf, last, in_think = "", "", time.time(), False

        def flush():  # the LLM pane gets the text as it comes, in small batches
            nonlocal buf, last
            if buf:
                emit({"t": "w", "b": "log", "text": buf, "raw": True})
                buf, last = "", time.time()
        try:
            for chunk in resp:
                if cancel.is_set():
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
            if cancel.is_set():
                emit({"t": "w", "b": "log", "text": " ✖ cancelled", "raw": True})
            emit({"t": "w", "b": "log", "text": "", "eos": True})
            if cancel.is_set():
                try:
                    resp.close()
                except Exception:
                    pass
        return out

    @staticmethod
    def image_part(img: str) -> dict:
        """A PNG for the model: the base64 the pad's export returns inline (nothing on disk), or a file path."""
        if len(img) < 4096 and os.path.isfile(img):
            with open(img, "rb") as f:
                img = base64.b64encode(f.read()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}

    def see(self, system: str, question: str, images, cancel: threading.Event, stream=True, max_tokens=700,
            think=False) -> str:
        """The eye: images (inline PNG base64, or paths) + question -> text. Streamed into the LLM pane, or one block (glances)."""
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        if not self.has_vision():
            raise RuntimeError("the LLM has no vision (llama-server needs --mmproj)")
        content = [self.image_part(p) for p in images] + [{"type": "text", "text": question}]
        # no system turn when there is none: for box_2d lists gemma places boxes better without one
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        if stream:
            return self.stream(messages, cancel, max_tokens=max_tokens, think=think)
        extra = {} if think else {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, max_tokens=max_tokens,
                                                messages=messages, **extra)
        return (r.choices[0].message.content or "").strip()

    def json_call(self, system: str, user: str) -> Optional[dict]:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        content = r.choices[0].message.content or ""
        logw("── 🌐 network attention ──\n" + (content.strip()[-1500:] or "(empty)") + "\n", "Comment")
        return parse_llm_json(content)


# ---------------------------------------------------------------- the pad
class PadError(Exception):
    pass


class Pad:
    """The disp pad seen from outside: its control socket. Launched on demand (detached: it outlives disp)."""

    def __init__(self, binary: str, port: int, home: str, on_event):
        self.binary, self.port, self.home, self.on_event = binary, port, home, on_event
        self.sock = None
        self.wfile = None
        self.lock = threading.Lock()
        self.waiters = {}
        self.next_id = 0
        self.connected = False
        self.focus = None  # {n, name, w, h, ...} as the pad last told
        self.count = 0
        # events are handled on their own thread, in order: a handler may ask the pad something (the reader thread
        # must stay free to read the answer)
        self.events: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._dispatch, daemon=True).start()

    def _dispatch(self):
        while True:
            msg = self.events.get()
            try:
                self.on_event(msg)
            except Exception as e:
                chat(f"⚠️ pad event error: {type(e).__name__}: {e}", "ErrorMsg")

    def available(self) -> bool:
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def _connect_once(self) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=1.5)
        except OSError:
            return False
        s.settimeout(None)
        self.sock = s
        self.wfile = s.makefile("w", encoding="utf-8", newline="\n")
        self.connected = True
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
        return True

    def launch(self):
        if not self.available():
            raise PadError(f"pad not built: {self.binary} (build it: :DispBuild, or cargo build --release in disp/pad)")
        os.makedirs(self.home, exist_ok=True)
        log = open(os.path.join(self.home, "pad.log"), "a")
        # detached: the pad outlives disp and Neovim (Windows: its own process group, no console)
        extra = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        subprocess.Popen([self.binary, "--port", str(self.port), "--home", self.home], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, close_fds=True, **extra)

    def ensure(self, launch=True) -> bool:
        """Connected to a pad (launching one if asked and none runs)."""
        if self.connected:
            return True
        with self.lock:
            if self.connected:
                return True
            if self._connect_once():
                pass
            elif not launch:
                return False
            else:
                self.launch()
                end = time.time() + 10
                while time.time() < end and not self._connect_once():
                    time.sleep(0.25)
                if not self.connected:
                    raise PadError(f"the pad did not come up on port {self.port} (see {self.home}/pad.log)")
        try:
            r = self.request("hello", timeout=5)
            self.focus, self.count = r.get("focus"), r.get("count", 0)
        except PadError:
            pass
        self.on_event({"ev": "_connected"})
        return True

    def _reader(self, s):
        f = s.makefile("r", encoding="utf-8", errors="replace")
        try:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "ev" in msg:
                    if msg["ev"] in ("loaded", "focus") and isinstance(msg.get("image"), dict):
                        self.focus = msg["image"]
                        self.count = max(self.count, int(msg["image"].get("n") or 0))
                    self.events.put(msg)
                elif msg.get("id") in self.waiters:
                    w = self.waiters[msg["id"]]
                    w["res"] = msg
                    w["ev"].set()
        except OSError:
            pass
        finally:
            if self.sock is s:
                self.connected = False
                self.sock = None
                for w in list(self.waiters.values()):
                    w["ev"].set()
                self.events.put({"ev": "_disconnected"})

    def request(self, op: str, timeout: float = 15, launch: bool = True, **params) -> dict:
        if not self.ensure(launch=launch):
            raise PadError("the pad is not running (===r or :DispPad starts it)")
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            w = {"ev": threading.Event(), "res": None}
            self.waiters[rid] = w
            try:
                self.wfile.write(json.dumps(dict(params, id=rid, op=op)) + "\n")
                self.wfile.flush()
            except (OSError, AttributeError, ValueError) as e:
                self.waiters.pop(rid, None)
                self.connected = False
                raise PadError(f"pad connection lost: {e}")
        try:
            if not w["ev"].wait(timeout):
                raise PadError(f"the pad did not answer '{op}' in {timeout:.0f}s")
            res = w["res"]
            if res is None:
                raise PadError("the pad went away")
            if not res.get("ok"):
                raise PadError(str(res.get("err") or "pad error"))
            if op in ("open", "paste", "focus", "crop") and isinstance(res.get("image"), dict):
                self.focus = res["image"]
                self.count = max(self.count, int(res["image"].get("n") or 0))
            return res
        finally:
            self.waiters.pop(rid, None)

    def summary(self) -> dict:
        f = self.focus or None
        return {"connected": self.connected, "count": self.count,
                "focus": {k: f.get(k) for k in ("n", "name", "w", "h")} if f else None}


# ---------------------------------------------------------------- memory (gddr9b, disp_map.py)
import disp_map  # noqa: E402

STORE: Optional["disp_map.MapStore"] = None


def key_of(info: Optional[dict]) -> str:
    """How an image is named in the panes: <first 4 hex of its sha256>#<its number on the pad>."""
    info = info or {}
    return f"{str(info.get('sha') or '????')[:4]}#{info.get('n', '?')}"


def mem_of(info: Optional[dict]):
    return STORE.get(info.get("sha")) if (STORE and isinstance(info, dict) and info.get("sha")) else None


def caption_of(info: Optional[dict]) -> str:
    m = mem_of(info)
    return str((m.meta.get("caption") or m.meta.get("summary") or "") if m else "")


# ---------------------------------------------------------------- prompts
SYSTEM_PROMPT = """You are disp, the image agent of the user's Vim. Images live on the disp pad, a window the user sees: the image in focus on top, a strip of every image ever loaded at the bottom (#1, #2, ...). On the pad the user zooms, selects regions, draws numbered marks and touches pixels; you can do the same through actions. You cannot see pixels yourself: the "look" action hands the pad's view to a vision model (the eye) and returns its answer as text.

RULES
1. Each turn run ONE action, then read its result. Base every claim on results (look, read_text, pixel, colors), never on guesses.
2. "this image"/"it"/"here" = the image in focus ([PAD]); "this area"/"there" = the user's selection or latest touch. Images are numbers (#3 = "image":3).
3. Coordinates are pixels of the ORIGINAL image: x to the right, y down, region = [x, y, w, h]. To find where something is, use
   locate (it returns a box), then mark it so the user sees what you mean.
4. Ask the eye focused questions; for small details look at a region (it is enlarged). For exact colours use pixel/colors, not the eye.
5. When you need the user to point at something, use ask_user_point / ask_user_region instead of guessing.
6. Editor actions (insert_text into the current buffer, run_command, run_lua) and save_image need the user's approval unless auto mode is on.
   Network requests cannot use editor actions.
7. Finish with declare_result: "reply" is the concrete answer (what you saw, where: #image and coordinates, marks you drew).
8. Output ONLY one JSON object with only the fields the action needs.
9. "image" is optional in every action (default: the image in focus); naming another image brings it into focus on the pad,
   and the following actions apply to it.
10. [MAP] is what is already known about the image in focus: elements e1, e2 … (type, label, box [x,y w×h], colours),
   nested by containment, text blocks and relations. Answer from it when it is enough; use its ids as regions
   ("region":"e5", text blocks too: "b2") and marks ("id":"e5"); look / locate only for what it lacks; map_note what
   you discover. When the user picked components on the pad's grid, the request lists them: they are what the user means.

ACTIONS — "action_type" is exactly one of the names below; all other fields sit at the top level of the same object.
Example: {"action_type": "look", "image": 3, "question": "what does the error dialog say?", "reason": "..."}
Looking:
  {"action_type":"look","question":"..","image":3,"region":[x,y,w,h],"grid":true}
      image optional (default: in focus); region optional: [x,y,w,h] | "selection" | "view" (what the user sees zoomed)
  {"action_type":"locate","what":"the red stop button"}           the box of a thing, in pixels (two-pass: coarse, then zoomed)
  {"action_type":"read_text","image":3,"region":[x,y,w,h]}         transcribe the text in the image (kept with it)
  {"action_type":"compare","images":[2,3],"question":"what changed?"}
  {"action_type":"pixel","x":120,"y":40,"image":3}                 the exact colour of one pixel
  {"action_type":"colors","region":[x,y,w,h]}                     mean and dominant colours
  {"action_type":"images","last":12}                              the images in memory, with captions
  {"action_type":"pad_state"}                                     focus, zoom, visible part, selection, marks
The map (kept per image in memory):
  {"action_type":"map_image","image":3,"fresh":false}             build the map (inventory → detail → colors → structure →
                                                                   relations; 1-3 minutes) or get it when it exists
  {"action_type":"map_element","id":"e5"}                         one element: box, colours, words, parent, children, relations
  {"action_type":"map_find","query":"save button"}                elements whose type or label match
  {"action_type":"map_note","text":"..","ids":["e5"]}             keep a finding with the image; add "a","rel","b" for a relation
Showing on the pad:
  {"action_type":"open_image","path":"~/shots/a.png"}             also: "paths":[...] for several
  {"action_type":"paste_image"}                                   the image on the clipboard
  {"action_type":"focus","image":3}
  {"action_type":"zoom","region":[x,y,w,h]}   or "fit":true   or "level":2.0 (2 = 200 %)
  {"action_type":"select","region":[x,y,w,h]}
  {"action_type":"crop","region":[x,y,w,h]}                       the region becomes a new image
  {"action_type":"mark","shape":"rect|ellipse|arrow|line|point|text","points":[[x,y],[x,y]],"text":"label","color":"yellow"}
      or {"action_type":"mark","id":"e5"} to box a map element
      rect/ellipse: two corners; arrow/line: from, to; point/text: one point. Marks are numbered on the image.
  {"action_type":"unmark","ids":[2]}                               (no ids: every mark on the image)
  {"action_type":"ask_user_point","prompt":"click the broken key"}  waits until the user touches a pixel on the pad
  {"action_type":"ask_user_region","prompt":"select the chart"}     waits until the user selects a region
  {"action_type":"save_image","path":"~/out.png","region":[x,y,w,h],"marks":true}
Editor:
  {"action_type":"insert_text","text":"..","where":"cursor|new"}   (new = a new scratch buffer)
  {"action_type":"run_command","command":"<Ex command>"}   {"action_type":"run_lua","code":"<Lua>"}
Network:
  {"action_type":"who"}   {"action_type":"ask_agent","to":"memphia","text":"..","wait":120}
  {"action_type":"wait_agent","from":"memphia","wait":120}   {"action_type":"post","text":"..","to":["name"]}
Finish:
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
Every action also takes "reason": one short sentence why.
"""

EYE_SYSTEM = """You are the eye of disp: you look at the image and answer the question about it precisely and briefly.
Report only what is visible; say so when something is unclear or too small. {coords}
Numbered marks (boxes, arrows, points with a number) were drawn by the user or by disp; refer to them by number."""

GLANCE_QUESTION = ("Describe this image in one or two sentences (what it is, the main content). "
                   "If it contains readable text, quote the most important text after 'Text:'.")

ACTION_NAMES = ["map_image", "map_element", "map_find", "map_note", "look", "locate", "read_text", "compare", "pixel", "colors", "images", "pad_state", "open_image", "paste_image",
                "focus", "zoom", "select", "crop", "mark", "unmark", "ask_user_point", "ask_user_region", "save_image",
                "insert_text", "run_command", "run_lua", "who", "ask_agent", "wait_agent", "post", "declare_result"]
ACTION_ALIASES = {"map": "map_image", "build_map": "map_image", "element": "map_element", "find_element": "map_find",
                  "search_map": "map_find", "note": "map_note", "relate": "map_note", "find": "locate", "where": "locate", "detect": "locate", "see": "look", "view": "look", "inspect": "look", "describe": "look", "analyze": "look",
                  "analyse": "look", "ocr": "read_text", "transcribe": "read_text", "diff": "compare",
                  "color": "colors", "stats": "colors", "list_images": "images", "state": "pad_state", "open": "open_image",
                  "load_image": "open_image", "show_image": "open_image", "paste": "paste_image", "annotate": "mark",
                  "draw": "mark", "clear_marks": "unmark", "erase": "unmark", "ask_point": "ask_user_point",
                  "ask_region": "ask_user_region", "export": "save_image", "insert": "insert_text"}


def normalize_action(act: dict) -> dict:
    """Accept near-misses small models produce: "action":"look", "pad.look", {"action_type":"pad","look":{...}}."""
    kind = str(act.get("action_type") or act.get("action") or "").strip()
    kind = ACTION_ALIASES.get(kind, kind)
    if kind in ACTION_NAMES:
        return dict(act, action_type=kind)
    for sep in (":", ".", "/", " "):
        tail = kind.rsplit(sep, 1)[-1].strip()
        tail = ACTION_ALIASES.get(tail, tail)
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


def as_region(v, state: Optional[dict] = None, element=None):
    """[x,y,w,h] from a list, a dict, "x,y,w,h", the words selection / view (from the pad state), or a map element id
    (e5, e5.w2: element(id) -> box)."""
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, str):
        w = v.strip().lower()
        if re.match(r"^(e\d+(\.w\d+)?|b\d+)$", w):
            box = element(w) if element else None
            if not box:
                raise ValueError(f"no element {w} in the map of the image in focus")
            return [round(x) for x in box]
        if w in ("selection", "sel", "selected"):
            return (state or {}).get("selection") or None
        if w in ("view", "visible", "screen"):
            return (state or {}).get("visible") or None
        if w in ("all", "whole", "full", "image"):
            return None
        v = [x for x in re.split(r"[,\s]+", w) if x]
    if isinstance(v, dict):
        v = [v.get("x"), v.get("y"), v.get("w", v.get("width")), v.get("h", v.get("height"))]
    try:
        r = [float(x) for x in v]
    except (TypeError, ValueError):
        raise ValueError("region must be [x, y, w, h] in image pixels")
    if len(r) != 4:
        raise ValueError("region must be [x, y, w, h] in image pixels")
    return [round(x) for x in r]


def image_arg(a: dict):
    v = a.get("image", a.get("n", a.get("img")))
    if isinstance(v, str):
        m = re.match(r"^\s*(?:[0-9a-f]{4})?#?(\d+)\s*$", v.strip(), re.I)  # 3, #3, 3fa2#3
        if m:
            return int(m.group(1))
    return v


def describe(info: Optional[dict]) -> str:
    if not info:
        return "(none)"
    src = info.get("source") or ""
    where = f" {info.get('path')}" if info.get("path") else ""
    return f"{key_of(info)} {info.get('name')} {info.get('w')}×{info.get('h')} ({src}{where})"


# ---------------------------------------------------------------- agent
class Cancelled(Exception):
    pass


class Agent:
    def __init__(self, args):
        self.args = args
        self.llm = LLM(args.host, args.port)
        self.pad = Pad(args.pad_bin, args.pad_port, args.memory, self.on_pad_event)
        self.running = True
        self.tasks: "queue.Queue" = queue.Queue()
        self.pending = 0
        self.current = None  # (text, origin)
        self.phase = "idle"
        self.cancel = threading.Event()
        self.auto = args.auto or autopilot_allows()
        self.glance = not args.no_glance
        self.glances: "queue.Queue" = queue.Queue()
        self.approval = None  # {"text":..., "event": Event, "answer": str}
        self.recent: deque = deque(maxlen=6)
        self.touches: deque = deque(maxlen=4)  # the user's latest touches on the pad
        self.user_waits: "queue.Queue" = queue.Queue()  # pad events while an ask_user_* waits
        self.waiting_user = None  # "click" | "select" while an ask_user_* waits
        self.reqs = {}
        self.req_id = 0
        self.rlock = threading.Lock()
        self.by_n = {}  # pad number -> image info (sha, name, size ...)
        self.layer_sent = {}  # sha -> signature of the grid layer the pad has
        self.map_auto = not args.no_map_auto
        self.maps: "queue.Queue" = queue.Queue()  # (info, fresh, done event | None)
        self.map_cancel = threading.Event()
        self.mapping = None  # key of the image being mapped
        self.mapper = disp_map.Mapper(self.map_eye, lambda op, **kw: self.pad.request(op, launch=False, timeout=30, **kw),
                                      lambda t: logw(t, "ErrorMsg"), detail_passes=args.map_passes)
        from disp_iface import IFace
        self.iface = IFace(self, args, self.llm.json_call, iface_log)

    # ----- state
    @property
    def busy(self):
        return self.current is not None

    def has_net_work(self):
        return bool(self.current and self.current[1]) or self.pending > 0

    def describe_busy(self):
        return (f"busy with: {clip(self.current[0], 160)}" if self.current else "idle") + f"; {self.pending} queued"

    def pad_brief(self) -> str:
        p = self.pad.summary()
        if not p["connected"]:
            return "the pad is closed"
        return f"{p['count']} images on the pad, in focus: {describe(self.pad.focus)}"

    def emit_state(self):
        pad = self.pad.summary()
        if pad.get("focus") and self.pad.focus:
            pad["focus"]["sha"] = self.pad.focus.get("sha")
        emit({"t": "state", "busy": self.busy, "phase": self.phase, "queue": self.pending, "auto": self.auto,
              "approval": self.approval["text"] if self.approval else None, "net": self.iface.summary(),
              "model": self.llm.model, "pad": pad, "glance": self.glance, "map_auto": self.map_auto,
              "mapping": self.mapping})

    def set_phase(self, p):
        self.phase = p
        self.emit_state()

    # ----- the pad's events (what the user does there): shown in DISP, kept in the image's trace (gddr9b)
    def info_of(self, n) -> dict:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return {}
        if n not in self.by_n:
            try:
                for i in self.pad.request("list", launch=False, timeout=5).get("images") or []:
                    self.by_n[i["n"]] = i
            except PadError:
                pass
        return self.by_n.get(n) or {}

    def push_layer(self, info: Optional[dict], mem):
        """The map as the pad's grid layer (dots + links), sent when it changed since the pad last got it."""
        m = mem.map
        sig = (m.get("updated"), len(m.get("elements") or []), len(m.get("relations") or []), len(m.get("blocks") or []))
        if not m.get("elements") or self.layer_sent.get(mem.sha) == sig or not self.pad.connected:
            return
        nodes, links = disp_map.layer(mem)
        try:
            self.pad.request("layer", image=info.get("n"), nodes=nodes, links=links, launch=False, timeout=10)
            self.layer_sent[mem.sha] = sig
        except PadError as e:
            if "unknown op" in str(e):  # an older pad: restart DISPAD for the grid layer
                self.layer_sent[mem.sha] = sig
                logw("pad has no grid layer (an older DISPAD: close it, disp opens the new one)", "WarningMsg")

    def emit_map(self, info: Optional[dict], focus: bool = False):
        """Send an image's MAP pane (disp.lua keeps one buffer per image, titled <sha4>#<n>), and its grid layer to
        the pad."""
        mem = mem_of(info)
        if not mem:
            return
        self.push_layer(info, mem)
        try:
            lines = disp_map.render(mem, info, mem.status)
        except Exception as e:
            lines = [f"(map not shown: {type(e).__name__}: {e})"]
        m = mem.map
        if mem.status:
            state = f"⏳ {mem.status}"
        elif m.get("elements"):
            state = f"{len(m['elements'])} elements" + (f" · {m['kind']}" if m.get("kind") else "")
        else:
            state = "no map yet"
        emit({"t": "map", "key": key_of(info), "n": info.get("n"), "sha": info.get("sha"), "lines": lines, "focus": focus,
              "title": f"{info.get('name')} {info.get('w')}×{info.get('h')} · {state}"})

    def remember(self, mem):
        """What memory knows about an image that comes back (same sha256): said in DISP, shown in its MAP pane."""
        m, me = mem.map, mem.meta
        parts = []
        if m.get("elements"):
            parts.append(f"map {len(m['elements'])} elements" + ("" if "relations" in (m.get("stages") or []) else " (partial)"))
        n_q, n_f = len(mem.notes.get("qa") or []), len(mem.notes.get("findings") or [])
        if n_q or n_f:
            parts.append(f"{n_q} questions, {n_f} findings")
        n_t = (me.get("trace") or {}).get("events") or 0
        if n_t:
            parts.append(f"{n_t} interactions")
        if parts:
            chat(f"   ↺ known: {' · '.join(parts)} · seen {me.get('opens') or 1}×, first "
                 f"{str(me.get('first_seen') or '')[:16].replace('T', ' ')}", "Comment")
        cap = me.get("summary") or me.get("caption")
        if cap:
            chat(f"   👁 {clip(cap, 300)}", "Comment")

    def on_pad_event(self, msg: dict):
        ev = msg.get("ev")
        img = msg.get("image") if isinstance(msg.get("image"), dict) else {}
        if img.get("n"):
            self.by_n[img["n"]] = img
        if ev == "_connected":
            self.layer_sent.clear()  # a (new) pad has no layers yet
            chat(f"🖼 pad connected (port {self.pad.port}) · {self.pad.count} images in memory"
                 + (f" · in focus {describe(self.pad.focus)}" if self.pad.focus else ""), "Comment")
            f = self.pad.focus
            if f:
                self.by_n[f.get("n")] = f
                mem = mem_of(f)
                if mem:
                    mem.seen(f, count_open=False)
                    self.remember(mem)
                self.emit_map(f, focus=True)
                self.want_map(f)
        elif ev == "bye":
            self.pad_bye = True
            chat("🖼 pad closed (===r or :DispPad opens it again)", "Comment")
        elif ev == "_disconnected":
            if self.running and not getattr(self, "pad_bye", False):
                chat("⚠️ lost the pad (===r or :DispPad opens it again)", "WarningMsg")
            self.pad_bye = False
        elif ev == "loaded":
            how = "new" if msg.get("new") else "known"
            by = "on the pad" if msg.get("by") == "pad" else "by disp"
            chat(f"🖼 {describe(img)} · {how}, loaded {by}", "Special")
            mem = mem_of(img)
            if mem:
                mem.seen(img, count_open=True)
                mem.add_trace({"ev": "open", "name": img.get("name"), "source": img.get("source"), "by": msg.get("by"),
                               "n": img.get("n")})
                if img.get("source") == "crop" and img.get("parent"):
                    pm = STORE.get(img["parent"])
                    if pm:
                        pm.add_trace({"ev": "crop", "child": key_of(img), "child_sha": img.get("sha")})
                self.remember(mem)
            self.emit_map(img, focus=True)
            if not caption_of(img) and self.glance:
                self.glances.put(img)
            self.want_map(img)
        elif ev == "focus":
            self.emit_map(img, focus=True)
            self.want_map(img)
        elif ev in ("click", "select", "mark", "unmark", "view", "ask", "pick", "grid"):
            info = self.info_of(msg.get("n")) if msg.get("n") else (self.pad.focus or {})
            mem = mem_of(info)
            rec = {"ev": ev}
            if ev == "click":
                hit = mem.hit(msg.get("x") or 0, msg.get("y") or 0) if mem else None
                t = {"n": msg.get("n"), "x": msg.get("x"), "y": msg.get("y"), "hex": msg.get("hex"), "ts": time.time(),
                     "hit": hit["id"] if hit else None, "label": hit.get("label") if hit else None}
                self.touches.append(t)
                chat(f"👆 {key_of(info)} ({t['x']}, {t['y']}) {t['hex']}"
                     + (f" on {hit['id']} {hit['type']} \"{clip(hit.get('label'), 60)}\"" if hit else ""), "Identifier")
                rec.update(x=t["x"], y=t["y"], hex=t["hex"], button=msg.get("button"))
                if hit:
                    rec.update(hit=hit["id"], hit_label=clip(hit.get("label"), 80))
                    emit({"t": "map_hit", "key": key_of(info), "id": hit["id"]})
                if self.waiting_user == "click":
                    self.user_waits.put(msg)
            elif ev == "select":
                inside = mem.inside(msg.get("region") or [0, 0, 0, 0]) if mem else []
                more = f" (+{len(inside) - 8})" if len(inside) > 8 else ""
                chat(f"▭ {key_of(info)} selection {msg.get('region')}"
                     + (f" ⊇ {' '.join(e['id'] for e in inside[:8])}{more}" if inside else ""), "Identifier")
                rec.update(region=msg.get("region"), inside=[e["id"] for e in inside[:30]])
                if self.waiting_user == "select":
                    self.user_waits.put(msg)
            elif ev == "mark":
                mk = msg.get("mark") or {}
                if mk.get("by") == "user":
                    chat(f"✏ {key_of(info)} mark {mk.get('id')} {mk.get('shape')} {mk.get('pts')}"
                         + (f" “{mk.get('text')}”" if mk.get("text") else ""), "Identifier")
                rec.update(id=mk.get("id"), shape=mk.get("shape"), pts=mk.get("pts"), text=mk.get("text"), by=mk.get("by"))
            elif ev == "unmark":
                rec.update(ids=msg.get("ids") or "all")
            elif ev == "view":
                rec.update(zoom=round(float(msg.get("zoom") or 0) * 100), visible=msg.get("visible"))
            elif ev == "pick":
                ids = [str(i) for i in msg.get("ids") or []]
                new = msg.get("added") or ([msg["id"]] if msg.get("id") else [])
                rec.update(id=msg.get("id"), added=msg.get("added"), on=msg.get("on"), ids=ids)
                if mem and new and msg.get("on"):
                    chat(f"⦿ {key_of(info)} {mem.ref_line(new[-1])}" + (f"  (+{len(new) - 1})" if len(new) > 1 else "")
                         + f" · picked: {' '.join(ids)}", "Identifier")
                    emit({"t": "map_hit", "key": key_of(info), "id": new[-1]})
                elif not ids:
                    chat(f"⦿ {key_of(info)} picks cleared", "Comment")
            elif ev == "grid":
                rec.update(on=msg.get("on"))
            elif ev == "ask":
                text = str(msg.get("text") or "").strip()
                refs = [str(r) for r in msg.get("refs") or []]
                rec.update(text=text, region=msg.get("region"), refs=refs or None)
                if text:
                    chat("")
                    chat(f"▶ (pad {key_of(info)}{' · ' + ' '.join(refs) if refs else ''}) {text}", "Title")
                    if self.busy:
                        chat("  (queued after the current task)", "Comment")
                    request = text
                    if refs and mem:
                        # what the user picked on the grid goes into the request itself
                        request += "\n(the user picked these on the pad's grid of " + key_of(info) + ":\n" + \
                                   "\n".join("  " + mem.ref_line(r) for r in refs[:24]) + ")"
                    self.submit(request, None, {"from_pad": True, "image_n": msg.get("n"), "region": msg.get("region"),
                                                "refs": refs})
                    emit({"t": "show"})
            if mem:
                mem.add_trace(rec)
                self.emit_map(info, focus=info.get("sha") == (self.pad.focus or {}).get("sha"))
        self.emit_state()

    # ----- the map: built in the background, once per content
    def want_map(self, info):
        mem = mem_of(info)
        if self.map_auto and mem and not set(disp_map.STAGES) <= set(mem.map.get("stages") or []):
            self.maps.put((info, False, None))

    def map_eye(self, question, png_b64, max_tokens, name=""):
        text = self.llm.see("", question, [png_b64], self.map_cancel, stream=False, max_tokens=max_tokens)
        logw(f"── 👁 map eye {name} ──\n{text[:1500]}" + ("\n…" if len(text) > 1500 else ""), "Comment")
        return text

    def map_worker(self):
        while self.running:
            try:
                info, fresh, done = self.maps.get(timeout=0.5)
            except queue.Empty:
                continue
            mem = mem_of(info)
            if not mem or (not fresh and set(disp_map.STAGES) <= set(mem.map.get("stages") or [])):
                if done:
                    done.set()
                continue
            if not self.llm.ensure() or not self.llm.has_vision():
                chat(f"🗺 {key_of(info)} not mapped: the LLM has no vision (llama-server needs --mmproj)", "WarningMsg")
                if done:
                    done.set()
                continue
            self.map_cancel.clear()
            key = key_of(info)
            self.mapping = key
            self.emit_state()
            chat(f"🗺 mapping {key} in the background (the MAP pane fills as it goes; ===x stops it)", "Comment")
            t0 = time.time()

            def progress(text):
                logw(f"── 🗺 {key} {text}", "Comment")
                if not re.match(r"^\d/5 ", text):
                    chat(f"   🗺 {key} {text}", "Comment")
                self.emit_map(info, focus=info.get("sha") == (self.pad.focus or {}).get("sha"))
            try:
                self.mapper.build(mem, info, self.map_cancel, progress, fresh=fresh)
                m = mem.map
                chat(f"🗺 {key} mapped in {int(time.time() - t0)}s: {len(m.get('elements') or [])} elements, "
                     f"{len(m.get('blocks') or [])} text blocks, {len(m.get('relations') or [])} relations"
                     + (f" — {clip(m.get('summary'), 200)}" if m.get("summary") else ""), "DiagnosticOk")
            except InterruptedError:
                chat(f"🗺 {key} mapping stopped (what was found is kept; ===m continues)", "WarningMsg")
            except Exception as e:
                chat(f"🗺 {key} mapping failed: {type(e).__name__}: {e}", "ErrorMsg")
            finally:
                mem.status = ""
                self.mapping = None
                self.emit_map(info, focus=info.get("sha") == (self.pad.focus or {}).get("sha"))
                self.emit_state()
                if done:
                    done.set()

    def map_go(self, msg):
        """<CR> / m / M in a MAP pane: show an element on the pad, mark it, or build the map again."""
        info = self.info_of(msg.get("image"))
        mem = mem_of(info)
        if not mem:
            return
        op, eid = msg.get("op"), str(msg.get("id") or "")
        try:
            if op == "grid":
                if (self.pad.focus or {}).get("sha") != info.get("sha"):
                    self.pad.request("focus", image=info.get("n"))
                r = self.pad.request("grid")
                return chat(f"▦ grid layer on the pad: {'on' if r.get('on') else 'off'}", "Comment")
            if op == "build":
                chat(f"🗺 {key_of(info)}: mapping again from scratch", "Comment")
                return self.maps.put((info, True, None))
            e = mem.block(eid) if eid.startswith("b") else mem.element(eid)
            if not e:
                return
            if (self.pad.focus or {}).get("sha") != info.get("sha"):
                self.pad.request("focus", image=info.get("n"))
            x, y, w, h = e["box"]
            if op == "mark":
                self.pad.request("mark", image=info.get("n"), shape="rect", region=[x, y, w, h],
                                 text=f"{eid} {clip(e.get('label') or e.get('t') or e.get('text'), 30)}", by="agent")
            else:
                mg = max(w, h) * 0.6 + 20
                self.pad.request("select", image=info.get("n"), region=[x, y, w, h])
                self.pad.request("view", region=[max(0, x - mg), max(0, y - mg), w + 2 * mg, h + 2 * mg])
            self.pad.request("raise")
        except PadError as ex:
            chat(f"⚠️ pad: {ex}", "ErrorMsg")

    # ----- glances: a caption for every new image
    def glance_worker(self):
        while self.running:
            try:
                img = self.glances.get(timeout=0.5)
            except queue.Empty:
                continue
            mem = mem_of(img)
            if not self.glance or not mem or mem.meta.get("caption"):
                continue
            try:
                ex = self.pad.request("export", image=img.get("n"), max=640, marks=False, launch=False)
                text = self.llm.see(EYE_SYSTEM.format(coords=""), GLANCE_QUESTION, [ex["png_b64"]], threading.Event(),
                                    stream=False, max_tokens=220)
                caption, _, seen = text.partition("Text:")
                with mem.lock:
                    mem.meta["caption"] = caption.strip()
                    if seen.strip() and not mem.meta.get("text"):
                        mem.meta["text"] = seen.strip()
                    mem.save("meta.yaml")
                logw(f"── 👁 glance {key_of(img)} ──\n{text}\n", "Comment")
                chat(f"   👁 {key_of(img)}: {clip(caption.strip(), 300)}", "Comment")
                if seen.strip():
                    chat(f"      text: {clip(seen.strip(), 200)}", "Comment")
                self.emit_map(img, focus=img.get("sha") == (self.pad.focus or {}).get("sha"))
            except Exception as e:
                logw(f"── 👁 glance {key_of(img)} failed: {type(e).__name__}: {e}", "ErrorMsg")

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
        if text in ("===a", ":auto"):
            self.auto = not self.auto
            chat(f"auto-approve editor actions: {'ON' if self.auto else 'off'}", "WarningMsg")
            return self.emit_state()
        if text in ("===x", ":cancel"):
            return self.do_cancel()
        if text in ("===m", "===m!"):
            f = self.pad.focus
            if not f:
                return chat("no image in focus", "WarningMsg")
            chat(f"🗺 {key_of(f)}: {'mapping again from scratch' if text.endswith('!') else 'mapping (missing stages)'}", "Comment")
            return self.maps.put((f, text.endswith("!"), None))
        if text == "===G":
            return threading.Thread(target=lambda: self.pad_grid(), daemon=True).start()
        if text == "===M":
            self.map_auto = not self.map_auto
            chat(f"map new images automatically: {'ON' if self.map_auto else 'off'}", "WarningMsg")
            return self.emit_state()
        if text in ("===g",):
            self.glance = not self.glance
            chat(f"glance at new images: {'ON' if self.glance else 'off'}", "WarningMsg")
            return self.emit_state()
        if text.startswith("===n"):
            return self.net_command(text[4:])
        if text in ("===h", "?", ":help"):
            return chat(HELP_TEXT, "Comment")
        if text.startswith(("===o", "===p", "===f", "===i", "===r", "===q")):
            return self.pad_command(text[3], text[4:].strip())
        chat("")
        chat("▶ " + text, "Title")
        if self.busy:
            chat("  (queued after the current task)", "Comment")
        self.submit(text, None, ctx)

    def pad_command(self, c, arg):
        """===o <path> open · ===p paste · ===f <n> focus · ===i list · ===r raise/launch · ===q close the pad."""
        try:
            if c == "o":
                if not arg:
                    return chat("===o <path to an image>", "Comment")
                self.pad.request("open", path=resolve_path(arg))
                self.pad.request("raise")
            elif c == "p":
                self.pad.request("paste")
                self.pad.request("raise")
            elif c == "f":
                r = self.pad.request("focus", image=int(arg.lstrip("#")) if arg.lstrip("#").isdigit() else arg)
                chat(f"🖼 focus {describe(r.get('image'))}", "Comment")
            elif c == "i":
                r = self.pad.request("list", last=20)
                for i in r.get("images") or []:
                    self.by_n[i["n"]] = i
                    cap = caption_of(i)
                    chat(f"  {'▶' if i.get('focus') else ' '} {describe(i)}" + (f" — {clip(cap, 120)}" if cap else ""), "Comment")
                chat(f"  {r.get('count')} images ({self.args.memory})", "Comment")
            elif c == "r":
                self.pad.request("raise")
            elif c == "q":
                if self.pad.connected:
                    self.pad.request("quit", launch=False)
        except (PadError, ValueError) as e:
            chat(f"⚠️ pad: {e}", "ErrorMsg")
        self.emit_state()

    def pad_grid(self):
        try:
            r = self.pad.request("grid")
            chat(f"▦ grid layer on the pad: {'on' if r.get('on') else 'off'}", "Comment")
        except PadError as e:
            chat(f"⚠️ pad: {e}", "ErrorMsg")

    def do_cancel(self):
        if self.mapping:
            self.map_cancel.set()
            chat(f"✖ stopping the map of {self.mapping}…", "WarningMsg")
        if self.busy:
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

    # ----- editor requests
    def nvim(self, op, timeout=None, **params) -> dict:
        with self.rlock:
            self.req_id += 1
            rid = self.req_id
            ev = threading.Event()
            self.reqs[rid] = {"ev": ev, "res": None}
        emit(dict({"t": "req", "id": rid, "op": op}, **params))
        end = time.time() + (timeout or self.args.cmd_timeout)
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

    # ----- worker
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
                logw(f"▶ 🌐 #{origin.get('id')} @{origin.get('from')}: {clip(text, 200)}", "Title")
            else:
                logw(f"▶ {clip(text, 200)}", "Title")
            status, reply = "FAILED", ""
            try:
                status, reply = self.run_task(text, origin, ctx)
            except Cancelled:
                status, reply = "CANCELLED", "Cancelled."
            except Exception as e:
                status, reply = "FAILED", f"{type(e).__name__}: {e}"
                chat(f"❌ {reply}", "ErrorMsg")
            hl = "DiagnosticOk" if status == "SUCCESS" else "ErrorMsg"
            chat(f"{'✔' if status == 'SUCCESS' else '✖'} {status}", hl)
            if reply:
                chat(reply)
            if status == "SUCCESS" and not origin:
                self.recent.append(f"- {clip(text, 160)} → {clip(reply, 200)}")
                mem = mem_of(self.pad.focus)
                if mem and reply:
                    mem.add_qa(text, reply, by="user")
                    self.emit_map(self.pad.focus, focus=True)
            if origin:
                self.iface.answer(origin, status, reply)
                mem = mem_of(self.pad.focus)
                if mem and status == "SUCCESS" and reply:
                    mem.add_qa(text, reply, by="@" + str(origin.get("from")))
            self.current = None
            self.set_phase("idle")
            if self.iface.connected:
                self.iface.set_state(self.iface.idle_state())

    def pad_block(self, ctx) -> str:
        """[PAD]: what the user has in front of them on the pad, small enough for the 16k context."""
        try:
            st = self.pad.request("state", timeout=5, launch=False)
        except PadError:
            return "[PAD] closed (open_image / paste_image open it)"
        f = st.get("focus")
        lines = [f"[PAD] {st.get('count')} images in memory · tool {st.get('tool')} · zoom {round((st.get('zoom') or 0) * 100)}%"
                 + (" (fit)" if st.get("fit") else "")]
        if f:
            lines.append(f"  in focus: {describe(f)} · visible region {st.get('visible')}")
            mem = mem_of(f)
            me = mem.meta if mem else {}
            notes = mem.notes if mem else {}
            if me.get("caption"):
                lines.append(f"  caption: {clip(me['caption'], 300)}")
            if me.get("text"):
                lines.append(f"  text in it: {clip(me['text'], 300)}")
            if me.get("opens", 0) > 1:
                lines.append(f"  seen {me['opens']}× since {str(me.get('first_seen'))[:10]}"
                             + (f" as {', '.join((me.get('names') or [])[-3:])}" if me.get("names") else ""))
            if st.get("selection"):
                lines.append(f"  user selection: {st['selection']}")
            for m in (st.get("marks") or [])[-8:]:
                lines.append(f"  mark {m.get('id')} {m.get('shape')} {m.get('pts')}"
                             + (f" “{m.get('text')}”" if m.get("text") else "") + f" (by {m.get('by') or 'user'})")
            for k in (notes.get("findings") or [])[-4:]:
                lines.append(f"  finding: {clip(k.get('text'), 200)}" + (f" ({' '.join(k['ids'])})" if k.get("ids") else ""))
            for q in (notes.get("qa") or [])[-3:]:
                lines.append(f"  earlier Q on it: {clip(q.get('q'), 120)} → {clip(q.get('a'), 200)}")
            tr = [t for t in (mem.trace if mem else []) if t.get("ev") in ("click", "select", "mark", "ask")][-4:]
            for t in tr:
                lines.append(f"  earlier on the pad: {disp_map.trace_line(t)}")
            if mem:
                lines.append(f"[MAP {key_of(f)}]" + (f" (being built: {mem.status})" if mem.status else ""))
                lines.append(disp_map.compact(mem, f, budget=self.args.map_context))
        else:
            lines.append("  no image in focus")
        now = time.time()
        for t in list(self.touches)[-3:]:
            lines.append(f"  user touched #{t['n']} at ({t['x']}, {t['y']}) colour {t['hex']}"
                         + (f" on {t['hit']} \"{clip(t.get('label'), 60)}\"" if t.get("hit") else "") + f", {int(now - t['ts'])}s ago")
        if isinstance(ctx, dict) and ctx.get("from_pad"):
            lines.append(f"  (the request was typed on the pad, looking at #{ctx.get('image_n')}"
                         + (f", region {ctx.get('region')}" if ctx.get("region") else "") + ")")
        try:
            lst = self.pad.request("list", last=8, timeout=5, launch=False)
            others = [i for i in lst.get("images") or [] if not i.get("focus")]
            if others:
                lines.append("[IMAGES] (latest)")
                for i in others[-6:]:
                    cap = caption_of(i)
                    lines.append(f"  {describe(i)}" + (f" — {clip(cap, 100)}" if cap else ""))
        except PadError:
            pass
        return "\n".join(lines)

    def context_block(self, origin, ctx) -> str:
        parts = [f"[TODAY] {time.strftime('%Y-%m-%d')}", self.pad_block(ctx)]
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network ({'private' if origin.get('to') else 'in the room'}).")
        else:
            if ctx is None or (isinstance(ctx, dict) and ctx.get("from_pad")):
                r = self.nvim("context", timeout=5)
                vctx = r.get("out") if r.get("ok") else None
            else:
                vctx = ctx
            if isinstance(vctx, dict):
                pos = f"[USER IN VIM] buffer {vctx.get('buffer') or '?'}\n  line: {clip(vctx.get('line'), 200)}"
                if vctx.get("image"):
                    pos += f"\n  image path under the cursor: {vctx['image']}"
                parts.append(pos)
            if self.recent:
                parts.append("[EARLIER SUCCESSFUL REQUESTS]\n" + "\n".join(self.recent))
        if self.iface.connected:
            parts.append(f"[NETWORK] {self.iface.net.network} as @{self.iface.net.name}; members: {self.iface.net.who()}")
        else:
            parts.append("[NETWORK] not joined (network actions unavailable)")
        return "\n\n".join(parts)

    def run_task(self, text, origin, ctx):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self.context_block(origin, ctx) + f"\n\n[REQUEST]\n{text}"}]
        for turn in range(1, self.args.max_turns + 1):
            if self.cancel.is_set():
                raise Cancelled()
            self.set_phase(f"thinking {turn}/{self.args.max_turns}")
            logw(f"── turn {turn}/{self.args.max_turns} ──", "Comment")
            raw = self.llm.stream(self.trim(messages), self.cancel)
            if self.cancel.is_set():
                raise Cancelled()
            act = parse_llm_json(raw)
            messages.append({"role": "assistant", "content": raw})
            if not act or not (act.get("action_type") or act.get("action")):
                messages.append({"role": "user", "content": "System: reply with ONE JSON object with \"action_type\"."})
                chat("  (unparsable LLM reply, asking again)", "Comment")
                continue
            act = normalize_action(act)
            kind = str(act.get("action_type")).strip()
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
            except (PadError, ValueError) as e:
                evidence = f"Error: {e}"
            except Exception as e:
                evidence = f"Error: {type(e).__name__}: {e}"
            evidence = str(evidence)
            first = evidence.split("\n", 1)[0]
            chat(f"    → {clip(first, 240)}" + (f"  (+{evidence.count(chr(10))} lines)" if "\n" in evidence else ""),
                 "ErrorMsg" if evidence.startswith(("Error", "System: denied")) else "Comment")
            messages.append({"role": "user", "content": f"[RESULT of {kind}]\n{evidence[:self.args.max_output]}"})
        return "FAILED", f"Stopped after {self.args.max_turns} turns without a result."

    def trim(self, messages):
        """Keep the system prompt, the request and the latest turns within --max-history characters."""
        head, rest = messages[:2], messages[2:]
        budget = self.args.max_history
        out = []
        for m in reversed(rest):
            n = len(m["content"]) if isinstance(m["content"], str) else 2000
            if budget - n < 0 and len(out) >= 2:
                break
            budget -= n
            out.append(m)
        out.reverse()
        if len(out) < len(rest):
            out.insert(0, {"role": "user", "content": f"System: {len(rest) - len(out)} earlier messages dropped (context limit)."})
        return head + out

    # ----- actions
    def eye(self, image, question, region=None, grid=False, marks=True, max_side=896, kind="look") -> str:
        return self.eye_raw(image, question, region, grid, marks, max_side, kind)[0]

    def eye_raw(self, image, question, region=None, grid=False, marks=True, max_side=896, kind="look"):
        """The eye on the pad's export of (a region of) an image: (answer, export info)."""
        st = self.pad.request("state", timeout=5)
        region = as_region(region, st, self.element_box)
        ex = self.pad.request("export", image=image, region=region, max=max_side, marks=marks,
                              grid=self.grid_step(region, image, st) if grid else 0)
        img = self.pad_info(image)
        x0, y0, (_, _, rw, rh) = ex["x0"], ex["y0"], ex["region"]
        coords = (f"The picture shows region x={x0}..{x0 + rw}, y={y0}..{y0 + rh} of image #{ex['n']} "
                  f"({img.get('w')}×{img.get('h')} pixels), scaled ×{ex['scale']:.2f}: give positions in the original "
                  f"pixels (x right, y down).")
        if grid:
            coords += " Cyan grid lines are labelled with original pixel coordinates; use them."
        logw(f"── 👁 {kind} #{ex['n']}" + (f" region {ex['region']}" if region else "") + f": {clip(question, 160)} ──", "Comment")
        self.set_phase(f"eye #{ex['n']}")
        answer = self.llm.see(EYE_SYSTEM.format(coords=coords), question, [ex["png_b64"]], self.cancel,
                              think=self.args.eye_think)
        if self.cancel.is_set():
            raise Cancelled()
        return answer.strip(), ex

    # gemma answers boxes the Gemini way whatever it is told: box_2d [ymin, xmin, ymax, xmax] on a 0-1000 scale of the
    # picture it sees. So it is asked exactly that (no grid: it confuses the model), and the box is mapped back here.
    LOCATE_Q = ('Detect {what}. Output JSON {{"box_2d": [ymin, xmin, ymax, xmax]}} normalized to 0-1000, '
                'or {{"box_2d": null}} if it is not in the picture.')

    def locate(self, what: str):
        """Coarse to fine: the whole image, then the found box enlarged (+30 % margin). -> (box x1,y1,x2,y2 | None, note)."""
        info = self.pad.focus or {}
        W, H = float(info.get("w") or 0), float(info.get("h") or 0)
        region, box, passes = None, None, 0
        for _ in range(2):
            ans, ex = self.eye_raw(None, self.LOCATE_Q.format(what=what), region, grid=False, marks=False, kind="locate")
            m = re.search(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]", ans)
            if not m:
                break
            yn1, xn1, yn2, xn2 = [min(1000.0, max(0.0, float(v))) for v in m.groups()]
            rx, ry, rw, rh = ex["region"]
            x1, x2 = sorted((rx + xn1 / 1000 * rw, rx + xn2 / 1000 * rw))
            y1, y2 = sorted((ry + yn1 / 1000 * rh, ry + yn2 / 1000 * rh))
            if x2 - x1 < 1 or y2 - y1 < 1:
                break
            box, passes = [round(x1), round(y1), round(x2), round(y2)], passes + 1
            mx, my = (x2 - x1) * 0.3 + 12, (y2 - y1) * 0.3 + 12
            nx1, ny1 = max(0.0, x1 - mx), max(0.0, y1 - my)
            nx2, ny2 = min(W or x2 + mx, x2 + mx), min(H or y2 + my, y2 + my)
            if (nx2 - nx1) * (ny2 - ny1) >= 0.8 * rw * rh:
                break  # the box fills the picture: a closer look would not help
            region = [round(nx1), round(ny1), round(nx2 - nx1), round(ny2 - ny1)]
        if not box:
            return None, "not found"
        return box, ("refined on a zoomed look" if passes > 1 else "coarse")

    def grid_step(self, region, image, st) -> int:
        info = self.pad_info(image) if image else (st.get("focus") or {})
        span = max(region[2], region[3]) if region else max(info.get("w") or 0, info.get("h") or 0)
        for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000):
            if span / step <= 10:
                return step
        return 5000

    def pad_info(self, image) -> dict:
        if image in (None, "", "focus"):
            return self.pad.focus or {}
        r = self.pad.request("list")
        for i in r.get("images") or []:
            if str(i.get("n")) == str(image).lstrip("#") or i.get("sha", "").startswith(str(image)) or i.get("name") == image:
                return i
        raise PadError(f"no image {image}")

    def element_box(self, eid):
        """The box of a map element (e5, e5.w2) or text block (b2) of the image in focus."""
        mem = mem_of(self.pad.focus)
        e = (mem.block(eid) if str(eid).startswith("b") else mem.element(eid)) if mem else None
        return e["box"] if e else None

    def map_action(self, kind, a) -> str:
        f = self.pad.focus
        mem = mem_of(f)
        if not mem:
            return "Error: no image in focus"
        if kind == "map_image":
            fresh = truthy(a.get("fresh"))
            if fresh or not set(disp_map.STAGES) <= set(mem.map.get("stages") or []):
                done = threading.Event()
                self.maps.put((f, fresh, done))
                self.set_phase(f"mapping {key_of(f)}")
                while not done.wait(0.3):
                    if self.cancel.is_set():
                        self.map_cancel.set()
                        raise Cancelled()
            return disp_map.compact(mem, f, budget=self.args.max_output - 200)
        if kind == "map_element":
            return disp_map.element_detail(mem, str(a.get("id") or a.get("element") or ""))
        if kind == "map_find":
            q = str(a.get("query") or a.get("text") or a.get("what") or "")
            hits = mem.find(q)
            if not mem.elements():
                return "the map is empty (map_image builds it)"
            return "\n".join(disp_map.el_line(e) for e in hits) or f"no element matches '{q}' (try look / locate)"
        if kind == "map_note":
            text = str(a.get("text") or "").strip()
            ids = [i for i in (a.get("ids") or []) if isinstance(i, str)]
            out = []
            if a.get("a") and a.get("rel") and a.get("b"):
                with mem.lock:
                    mem.map.setdefault("relations", []).append({"a": str(a["a"]), "rel": clip(a["rel"], 30), "b": str(a["b"]),
                                                                "by": "disp"})
                    mem.save("map.yaml")
                out.append(f"relation {a['a']} {a['rel']} {a['b']} kept")
            if text:
                mem.add_finding(text, ids, by="disp")
                out.append("finding kept")
            self.emit_map(f, focus=True)
            return "; ".join(out) or "Error: map_note needs \"text\" (and/or a, rel, b)"
        return f"Error: unknown map action {kind}"

    def wait_user(self, kind, prompt, wait, tool):
        prev = (self.pad.request("state", timeout=5) or {}).get("tool")
        self.pad.request("prompt", text=prompt, secs=wait)
        self.pad.request("tool", name=tool)
        self.pad.request("raise")
        chat(f"  ⏳ on the pad: {prompt}", "WarningMsg")
        while not self.user_waits.empty():
            self.user_waits.get_nowait()
        self.waiting_user = kind
        self.set_phase("waiting for you on the pad")
        end = time.time() + wait
        try:
            while time.time() < end:
                if self.cancel.is_set():
                    raise Cancelled()
                try:
                    return self.user_waits.get(timeout=0.3)
                except queue.Empty:
                    continue
            return None
        finally:
            self.waiting_user = None
            try:
                self.pad.request("prompt", text="")
                if prev:
                    self.pad.request("tool", name=prev)
            except PadError:
                pass

    FOCUSING = {"map_image", "map_element", "map_find", "map_note", "look", "locate", "read_text", "pixel", "colors", "zoom", "select", "crop", "mark", "unmark",
                "ask_user_point", "ask_user_region"}

    def bring(self, image):
        """An action names another image: the pad shows it, so the user sees what disp works on and the next
        actions (without "image") apply to it."""
        f = self.pad.focus or {}
        if image in (None, "", "focus") or str(image).lstrip("#") == str(f.get("n")):
            return
        r = self.pad.request("focus", image=image)
        chat(f"    🖼 focus {describe(r.get('image'))}", "Comment")

    def do_action(self, kind, a, origin):
        image = image_arg(a)
        if kind in self.FOCUSING:
            self.bring(image)
            image = None
        if kind in ("look", "read_text"):
            if kind == "read_text":
                q = "Transcribe all readable text in this picture exactly, line by line, keeping its order. Write NONE if there is no text."
            else:
                q = str(a.get("question") or a.get("prompt") or a.get("text") or "").strip() or "Describe what you see."
            grid = truthy(a.get("grid")) or (a.get("grid") is None and kind == "look" and
                                             re.search(r"\b(where|locat|position|coordinat|find|point|which part)", q, re.I))
            marks = truthy(a.get("marks", kind == "look"))  # marks would pollute a transcription
            ans = self.eye(image, q, a.get("region"), bool(grid), marks, kind=kind)
            mem = mem_of(self.pad.focus)
            if kind == "read_text" and not a.get("region") and mem:
                with mem.lock:
                    mem.meta["text"] = ans
                    mem.save("meta.yaml")
            return ans or "(the eye said nothing)"
        if kind.startswith("map_"):
            return self.map_action(kind, a)
        if kind == "locate":
            what = str(a.get("what") or a.get("target") or a.get("question") or a.get("text") or "").strip()
            if not what:
                return "Error: locate needs \"what\""
            box, how = self.locate(what)
            if not box:
                return f"the eye did not find {what} on #{(self.pad.focus or {}).get('n')}"
            x1, y1, x2, y2 = box
            return (f"{what} on #{(self.pad.focus or {}).get('n')}: box [{x1}, {y1}] to [{x2}, {y2}] "
                    f"(region [{x1}, {y1}, {x2 - x1}, {y2 - y1}], centre [{(x1 + x2) // 2}, {(y1 + y2) // 2}]; {how})")
        if kind == "compare":
            ims = a.get("images") or []
            if not isinstance(ims, list) or len(ims) < 2:
                return "Error: compare needs \"images\": [n1, n2]"
            paths = []
            for n in ims[:3]:
                ex = self.pad.request("export", image=n, max=640, marks=truthy(a.get("marks", True)))
                paths.append(ex["png_b64"])
            q = str(a.get("question") or "What is different between these images?")
            names = ", ".join(f"picture {i + 1} = image #{n}" for i, n in enumerate(ims[:3]))
            logw(f"── 👁 compare {names}: {clip(q, 160)} ──", "Comment")
            self.set_phase("eye: compare")
            return self.llm.see(EYE_SYSTEM.format(coords=f"There are {len(paths)} pictures: {names}."), q, paths,
                                self.cancel, think=self.args.eye_think).strip()
        if kind == "pixel":
            r = self.pad.request("pixel", image=image, x=a.get("x"), y=a.get("y"))
            return f"#{r['n']} pixel ({r['x']}, {r['y']}) = {r['hex']} rgba {r['rgba']}"
        if kind == "colors":
            st = self.pad.request("state", timeout=5)
            r = self.pad.request("stats", image=image, region=as_region(a.get("region") or a.get("id"), st, self.element_box))
            dom = ", ".join(f"{d['hex']} {d['share']}%" for d in r.get("dominant") or [])
            return f"region {r['region']}: mean {r['mean_hex']} (rgba {r['mean']}), min {r['min']}, max {r['max']}; dominant: {dom}"
        if kind == "images":
            r = self.pad.request("list", last=max(1, min(40, int(a.get("last") or 12))))
            out = [f"{'▶' if i.get('focus') else ' '} {describe(i)}" + (f" — {clip(caption_of(i), 140)}" if caption_of(i) else "")
                   for i in r.get("images") or []]
            return "\n".join(out + [f"({r.get('count')} images in memory)"]) or "no images yet"
        if kind == "pad_state":
            st = self.pad.request("state", timeout=5)
            return json.dumps({k: st.get(k) for k in ("focus", "count", "zoom", "fit", "visible", "selection", "tool", "marks")},
                              ensure_ascii=False)[:3000]
        if kind == "open_image":
            paths = a.get("paths") if isinstance(a.get("paths"), list) else [a.get("path")]
            out = []
            for p in [p for p in paths if p]:
                p = resolve_path(str(p))
                if not os.path.isfile(p):
                    out.append(f"Error: no such file {p}")
                    continue
                r = self.pad.request("open", path=p)
                out.append(f"shown on the pad: {describe(r.get('image'))}" + ("" if r.get("new") else " (already in memory)"))
            self.pad.request("raise")
            return "\n".join(out) or "Error: open_image needs \"path\""
        if kind == "paste_image":
            r = self.pad.request("paste")
            self.pad.request("raise")
            return f"pasted: {describe(r.get('image'))}" + ("" if r.get("new") else " (already in memory)")
        if kind == "focus":
            r = self.pad.request("focus", image=image)
            return f"in focus: {describe(r.get('image'))}"
        if kind == "zoom":
            st = self.pad.request("state", timeout=5)
            kw = {"image": image} if image else {}
            if truthy(a.get("fit")):
                kw["fit"] = True
            elif a.get("region"):
                kw["region"] = as_region(a.get("region"), st, self.element_box)
            elif a.get("level") or a.get("zoom"):
                kw["zoom"] = float(a.get("level") or a.get("zoom"))
                if a.get("x") is not None and a.get("y") is not None:
                    kw["center"] = [float(a["x"]), float(a["y"])]
            else:
                return "Error: zoom needs \"region\", \"fit\":true or \"level\""
            r = self.pad.request("view", **kw)
            return f"zoom {round(r['zoom'] * 100)}%, the user now sees region {r.get('visible')}"
        if kind == "select":
            r = self.pad.request("select", image=image, region=as_region(a.get("region") or a.get("id"), None, self.element_box))
            return f"selected {r.get('selection')}"
        if kind == "crop":
            st = self.pad.request("state", timeout=5)
            reg = as_region(a.get("region") or a.get("id"), st, self.element_box)
            r = self.pad.request("crop", image=image, **({"region": reg} if reg else {}))
            return f"new image {describe(r.get('image'))} (now in focus)"
        if kind == "mark":
            kw = {"image": image, "shape": a.get("shape") or "point", "by": "agent"}
            pts = a.get("points") or a.get("pts")
            if not pts and a.get("x") is not None:
                pts = [[a.get("x"), a.get("y")]]
            if a.get("region") or a.get("id"):
                kw["region"] = as_region(a.get("region") or a.get("id"), None, self.element_box)
                if a.get("id") and not a.get("shape"):
                    kw["shape"] = "rect"
                    fm = mem_of(self.pad.focus)
                    e = fm and (fm.block(a["id"]) if str(a["id"]).startswith("b") else fm.element(a["id"]))
                    kw.setdefault("text", a.get("text") or (f"{a['id']} {clip(e.get('label') or e.get('t') or e.get('text'), 30)}"
                                                            if e else a["id"]))
            if pts:
                kw["points"] = pts
            for k in ("text", "color", "width"):
                if a.get(k) is not None:
                    kw[k] = a[k]
            r = self.pad.request("mark", **kw)
            return f"mark {r['id']} drawn on #{r['n']}"
        if kind == "unmark":
            ids = a.get("ids")
            r = self.pad.request("unmark", image=image, **({"ids": ids} if isinstance(ids, list) else {}))
            return f"{r.get('removed')} marks removed"
        if kind in ("ask_user_point", "ask_user_region"):
            wait = max(10.0, min(float(a.get("wait") or self.args.user_timeout), 900.0))
            prompt = str(a.get("prompt") or a.get("text") or ("touch the pixel you mean" if kind == "ask_user_point"
                                                             else "select the region you mean"))
            if kind == "ask_user_point":
                ev = self.wait_user("click", prompt, wait, "touch")
                if not ev:
                    return f"System: the user touched nothing within {int(wait)}s."
                return f"the user touched #{ev.get('n')} at ({ev.get('x')}, {ev.get('y')}), colour {ev.get('hex')}"
            ev = self.wait_user("select", prompt, wait, "select")
            if not ev:
                return f"System: the user selected nothing within {int(wait)}s."
            return f"the user selected region {ev.get('region')} on #{ev.get('n')}"
        if kind == "save_image":
            if origin:
                return "System: denied — network requests cannot write files."
            path = resolve_path(str(a.get("path") or ""))
            if not a.get("path"):
                return "Error: save_image needs \"path\""
            if not path.lower().endswith(".png"):
                path += ".png"
            if not self.approve(f"write {path}"):
                return "System: denied by the user."
            st = self.pad.request("state", timeout=5)
            r = self.pad.request("export", image=image, region=as_region(a.get("region"), st, self.element_box), max=8192,
                                 marks=truthy(a.get("marks", True)), path=path)
            return f"saved {r['path']} ({r['w']}×{r['h']})"

        if kind in ("insert_text", "run_command", "run_lua"):
            if origin:
                return "System: denied — network requests cannot drive the user's editor."
            if kind == "insert_text":
                text = str(a.get("text") or "")
                if not text:
                    return "Error: insert_text needs \"text\""
                where = "new" if str(a.get("where") or "cursor").lower().startswith("new") else "cursor"
                if where == "cursor" and not self.approve(f"insert {len(text.splitlines())} lines at the cursor"):
                    return "System: denied by the user."
                r = self.nvim("insert", text=text, where=where)
                return str(r.get("out") or "inserted") if r.get("ok") else f"Error: {r.get('err')}"
            if kind == "run_command":
                cmd = str(a.get("command") or "").strip().lstrip(":")
                if not cmd:
                    return "Error: run_command needs \"command\""
                if not self.approve(f":{cmd}"):
                    return "System: denied by the user."
                r = self.nvim("ex", command=cmd)
            else:
                code = str(a.get("code") or "")
                if not code.strip():
                    return "Error: run_lua needs \"code\""
                if not self.approve("Lua: " + clip(code, 200)):
                    return "System: denied by the user."
                r = self.nvim("lua", code=code)
            return (str(r.get("out") or "(ok, no output)") if r.get("ok") else f"Error: {r.get('err')}")

        if kind == "who":
            return self.iface.net.who() if self.iface.connected else "System: not on the network."
        if kind == "ask_agent":
            text = str(a.get("text") or "").strip()
            if not text:
                return "Error: ask_agent needs \"text\""
            self.set_phase("waiting @" + str(a.get("to")))
            return self.iface.ask(str(a.get("to") or ""), text, a.get("wait") or self.args.ask_timeout, self.cancel,
                                  re=origin.get("id") if origin else None)
        if kind == "wait_agent":
            self.set_phase("waiting @" + str(a.get("from")))
            return self.iface.wait(str(a.get("from") or ""), a.get("wait") or self.args.ask_timeout, self.cancel)
        if kind == "post":
            to = a.get("to") if isinstance(a.get("to"), list) else None
            ok = self.iface.say(str(a.get("text") or ""), to=to)
            return "posted" if ok else "System: not sent (not connected or empty)."
        return (f"Error: unknown action_type '{kind}'. Use one of: {', '.join(ACTION_NAMES)} — "
                f"e.g. {{\"action_type\": \"look\", \"question\": \"..\"}}")


HELP_TEXT = """disp — images in Vim: the pad shows them, the eye (vision LLM) reads them
  <text>   a request: "what is in this image?", "read the text", "mark the red button", "zoom on the error",
           "compare #2 and #3", "crop the chart", "put the text of this screenshot in a new buffer" ...
  ===o <path> open on the pad   ===p paste from the clipboard   ===f <n> focus #n   ===i list images
  ===r show / start the pad     ===q close the pad              ===g glance at new images on/off
  ===m map the image in focus (missing stages)   ===m! map it again from scratch   ===M auto-map new images on/off
  ===G the pad's grid layer on/off: a dot per component (e5, b2), lines for relations; click dots to pick them,
       the picks go with the question you type on the pad
  ===a auto-approve editor actions   ===x cancel the running task (and a map being built)
  ===n network status   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
  The tab: MAP panes on top, one per image (<sha4>#<n>) · DISP (this chat) · LLM (raw LLM + eye) · IFACE (network)
  MAP: <CR> on an element shows it on the pad · m marks it · M maps again · G grid on the pad · X closes the pane
  <Tab> next pane · <S-Tab> previous · i input · <C-c> cancel · q back to your tab (normal mode)
  On the pad: right-click (or the touch tool) touches a pixel; select / marks / "Ask disp" reach this chat."""


def autopilot_allows() -> bool:
    path = os.path.expanduser("~/echo-home/autopilot.yaml")
    try:
        import yaml
        data = yaml.safe_load(open(path)) or {}
        v = (data.get("disp") or {}).get("allow")
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
        elif t == "res":
            agent.on_result(msg)
        elif t == "map":
            info = agent.info_of(msg.get("image"))
            if info:
                agent.emit_map(info, focus=False)
        elif t == "map_go":
            threading.Thread(target=agent.map_go, args=(msg,), daemon=True).start()
        elif t in ("open", "paste", "focus", "pad"):
            threading.Thread(target=pad_from_vim, args=(agent, msg), daemon=True).start()
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
    agent.running = False
    agent.map_cancel.set()
    if STORE:
        STORE.flush()
    agent.iface.net.close()
    emit({"t": "exit"})


def pad_from_vim(agent: Agent, msg: dict):
    """:DispOpen / :DispPaste / :DispFocus / :DispPad: straight to the pad, no LLM."""
    t = msg.get("t")
    if t == "open":
        agent.pad_command("o", str(msg.get("path") or ""))
    elif t == "paste":
        agent.pad_command("p", "")
    elif t == "focus":
        agent.pad_command("f", str(msg.get("image") or ""))
    elif t == "pad":
        agent.pad_command("q" if msg.get("op") == "quit" else "r", "")


def main():
    global ARGS
    p = argparse.ArgumentParser(description="disp: the image agent (backend of disp.lua)")
    p.add_argument("--pad-bin", default=os.environ.get("DISP_PAD_BIN") or os.path.join(
        HERE, "pad", "target", "release", "disppad.exe" if os.name == "nt" else "disppad"),
                   help="the pad binary (default: pad/target/release/disppad next to disp.py)")
    p.add_argument("--pad-port", type=int, default=int(os.environ.get("DISP_PAD_PORT") or 7791))
    p.add_argument("--no-pad-start", action="store_true", help="do not open the pad at startup (only on first use)")
    p.add_argument("--no-glance", action="store_true", help="no caption for new images")
    p.add_argument("--memory", "--home", dest="memory",
                   default=os.environ.get("DISP_MEMORY") or os.environ.get("DISP_HOME") or "~/echo-home/memory/gddr9b",
                   help="the memory disp and the pad share: per image its map, notes, trace and marks, the pad's strip; "
                        "images themselves stay where they are (default ~/echo-home/memory/gddr9b)")
    p.add_argument("--no-map-auto", action="store_true", help="do not map images when they are loaded (===m maps)")
    p.add_argument("--map-passes", type=int, default=3, help="zoomed detail looks per map (0: inventory only)")
    p.add_argument("--map-context", type=int, default=3200, help="characters of the map put in the LLM's context")
    p.add_argument("--eye-think", action="store_true", help="let the eye think before answering (slower)")
    p.add_argument("--port", type=int, default=8080, help="port of the local LLM server (llama.cpp with --mmproj)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--auto", action="store_true", help="auto-approve editor actions (also autopilot.yaml disp.allow)")
    p.add_argument("--max-turns", type=int, default=12)
    p.add_argument("--max-output", type=int, default=3500)
    p.add_argument("--max-history", type=int, default=22000, help="characters of turns kept for the LLM")
    p.add_argument("--cmd-timeout", type=int, default=30)
    p.add_argument("--user-timeout", type=int, default=120, help="seconds ask_user_point/region wait for the user")
    p.add_argument("--ask-timeout", type=int, default=180, help="seconds ask_agent waits for an answer")
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("DISP_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    ARGS = p.parse_args()
    ARGS.pad_bin = resolve_path(ARGS.pad_bin)
    ARGS.memory = resolve_path(ARGS.memory)
    global STORE
    STORE = disp_map.MapStore(ARGS.memory)

    agent = Agent(ARGS)
    threading.Thread(target=agent.worker, daemon=True).start()
    threading.Thread(target=agent.glance_worker, daemon=True).start()
    threading.Thread(target=agent.map_worker, daemon=True).start()
    chat(f"disp · images in Vim · memory {ARGS.memory}", "Title")
    llm_ok = agent.llm.ensure()
    vision = agent.llm.has_vision() if llm_ok else False
    chat(f"LLM {agent.llm.base}: " + ((agent.llm.model or "").rsplit("/", 1)[-1] + (" · vision ✔" if vision else
         " · ⚠️ NO vision: llama-server needs --mmproj") if llm_ok else
         "not reachable yet (I retry on each request)"),
         "Comment" if (llm_ok and vision) else "WarningMsg")
    if not agent.pad.available():
        chat(f"⚠️ the pad is not built: {ARGS.pad_bin}\n   :DispBuild (or cargo build --release in {HERE}/pad)", "WarningMsg")
    else:
        try:
            if not agent.pad.ensure(launch=not ARGS.no_pad_start):
                chat("🖼 the pad opens on first use (===r opens it now)", "Comment")
        except PadError as e:
            chat(f"⚠️ pad: {e}", "ErrorMsg")
    chat("Ask about the image in focus, or open one: dispo on a path, ===p paste. ===h help · <Tab> next pane", "Comment")
    logw("LLM — raw LLM output and what the eye sees, streamed", "Title")
    iface_log("IFACE — Intelligence Network traffic and decisions (shown while joined)", "Title")
    import disp_iface
    if disp_iface.INET_ERROR:
        iface_log("⚠️ Intelligence Network driver not loaded; disp works on images but cannot join.", "ErrorMsg")
        iface_log("   " + disp_iface.INET_ERROR, "ErrorMsg")
    else:
        iface_log(f"   inet driver: {disp_iface.INET_DIR}", "Comment")
    if ARGS.net_name:
        agent.iface.join()
    agent.emit_state()
    reader(agent)


if __name__ == "__main__":
    main()
