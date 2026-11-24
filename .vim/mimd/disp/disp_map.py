"""
disp_map: the image map of disp — what an image holds and how it is laid out, built once per content (sha256) and kept
in ~/echo-home/memory/gddr9b. Like a KV cache prefilled from the image: the picture is read once, into a structure
later questions read instead of looking again (the eye is only asked for what the map lacks).

Prefill pipeline (Mapper.build); each stage is saved and shown in the MAP pane as soon as it completes:
  1 inventory   the eye lists the elements of the whole picture — text lines, words, buttons, boxes, icons, shapes,
                figures, colour regions, patterns — each with its box_2d: a FLAT list
  2 detail      zoomed looks into the biggest containers (or the tiles of a large image) add what was too small
  3 pixels      every box snapped to the pixels (the eye's boxes are often tens of pixels off: the pad finds the blob
                of the element's colour around it) and its colours measured: mean, dominant, text fg / bg
  4 structure   a DOM-like TREE by containment; reading order; the words of each text line; text blocks (lines that
                read together vs labels standing apart); geometric relations (rows, left-of, above, overlaps)
  5 relations   the LLM looks at the picture and the tree together: kind, summary, groups, semantic relations
                (labels, points to, part of ...), sentences formed by separate words, what the list misses

Memory, one folder per content (layout of hbmx's memory, see README.md written at the root):
  <root>/<d1>/<d2>/<d3d4>/<sha256>/
      meta.yaml     identity (names, paths, size, the file it is read from), first/last seen, opens, caption, text,
                    map status
      map.yaml      elements (flat, with parent / colours / words), text blocks, relations, groups, summary
      notes.yaml    questions answered on the image (qa) and findings (by disp, the user, other agents)
      trace.jsonl   what the user did on the pad with this image: touches, selections, marks, asks, zooms, crops
Images are not copied: the pad reads each from its own path (catalog.jsonl at the root says where) and hashes it on
every load; only a pasted or cropped image, which has no file of its own, is kept once as <sha>/image.png. What the eye
was shown (a region, scaled) is cut from the image on demand; map.yaml `looks` records each region and scale.
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import yaml

MAP_VERSION = 1
STAGES = ["inventory", "detail", "pixels", "structure", "relations"]

README = """# gddr9b — disp's image memory

What disp (the image agent, `~/echo-home/.vim/mimd/disp`) and its pad DISPAD know about the images shown on the pad.
An image is known by its content: the `sha256` of its bytes, computed every time the pad loads it. The same picture
under another name or path reuses this memory; reopening it brings back its map, notes, marks and trace.
Images are not copied here: the pad reads each one from where it is.

```
catalog.jsonl                    the pad's strip: one line per load (sha, path, name, source, size, mtime, w, h, ext);
                                 the images in the order first loaded, the last line of a sha says where it is now
<d1>/<d2>/<d3d4>/                bucket: 1st, 2nd and 3rd-4th hex digits of the sha256
    <full sha256>/               one image
        meta.yaml                identity (names, paths, size), first/last seen, opens, caption, text read in it,
                                 map status                                                                   (disp)
        map.yaml                 the map: elements (flat list: id, type, label, box [x,y,w,h], colours, parent,
                                 words), text blocks, relations (geometric + semantic), groups, kind, summary,
                                 looks (the regions the eye was shown)                                         (disp)
        notes.yaml               qa (every question answered on it) and findings                               (disp)
        trace.jsonl              the user's interactions on the pad: touch, select, mark, unmark, ask, view, crop,
                                 pick, grid                                                                    (disp)
        marks.json               the marks drawn on it                                                          (pad)
        image.png                only for an image with no file of its own: a paste or a crop                  (pad)
```

The map is built in stages (inventory → detail → pixels → structure → relations), see disp_map.py. A box snapped
to the pixels is exact (`snapped: true`); the others are the eye's estimate (`eye_box` keeps the eye's own when snapped).
Element ids (e1, e2 …) are stable for an image; e0 is the image itself. Words of a text line: e7.w2. Text blocks: b1 …
Nothing derived is stored (no thumbnails, no crops shown to the eye): it is all made again from the image on demand.
Remove an image's memory: delete its `<full sha256>/` folder (a pasted image goes with it).
"""

TYPE_MAP = {
    "text": "text", "word": "text", "line": "text", "number": "text", "title": "text", "heading": "text",
    "label": "text", "paragraph": "text", "caption": "text", "digit": "text", "sentence": "text",
    "button": "button", "link": "button", "tab": "button", "menu": "button", "checkbox": "button", "toggle": "button",
    "box": "box", "panel": "box", "rect": "box", "rectangle": "box", "window": "box", "frame": "box", "card": "box",
    "field": "box", "input": "box", "table": "box", "container": "box", "dialog": "box", "toolbar": "box", "bar": "box",
    "icon": "icon", "logo": "icon", "symbol": "icon", "emoji": "icon",
    "shape": "shape", "circle": "shape", "square": "shape", "triangle": "shape", "arrow": "shape", "line_shape": "shape",
    "ellipse": "shape", "polygon": "shape", "star": "shape", "disc": "shape", "dot": "shape",
    "figure": "figure", "photo": "figure", "image": "figure", "picture": "figure", "chart": "figure", "graph": "figure",
    "thumbnail": "figure", "person": "figure", "object": "figure", "face": "figure", "animal": "figure", "diagram": "figure",
    "region": "region", "background": "region", "area": "region", "color": "region", "band": "region", "swatch": "region",
    "pattern": "pattern", "texture": "pattern", "grid": "pattern", "stripes": "pattern",
}
SHAPE_KINDS = ("circle", "square", "triangle", "arrow", "ellipse", "polygon", "star", "disc", "dot", "line", "rectangle")
CONTAINERS = ("box", "button", "figure", "region", "shape", "pattern")

INVENTORY_Q = (
    'List every distinct visible element in this picture{where}: text lines (with their exact text), standalone words or '
    'numbers, buttons, boxes and panels, icons, shapes (circle, square, arrow, line ...), photos or figures, colour '
    'regions, repeated patterns. Output ONLY a JSON array, one object per element: {{"type": "text|button|box|icon|'
    'shape|figure|region|pattern", "label": "<the exact text (max 80 characters), or a short name>", "box_2d": [ymin, '
    'xmin, ymax, xmax]}} with box_2d normalized to 0-1000. At most {most} elements, the most important first.')

RELATIONS_Q = (
    "Here is the inventory of the elements found in this picture: id, type, label, box [x,y w×h] in pixels of a {w}×{h} "
    "image, nested by containment.\n\n{tree}\n\n{blocks}"
    "Look at the picture and this list together and describe the higher-level structure. Output ONLY JSON:\n"
    '{{"kind": "screenshot|photo|document|diagram|chart|drawing|graphic|other",\n'
    ' "summary": "<2-3 sentences: what the image is and how it is organised>",\n'
    ' "groups": [{{"name": "<e.g. toolbar, form, legend, header>", "ids": ["e1", "e2"]}}],\n'
    ' "relations": [{{"a": "e3", "rel": "<labels|describes|points to|part of|controls|same as|next to|contains>", '
    '"b": "e7"}}],\n'
    ' "sentences": [{{"ids": ["e4", "e5"], "text": "<separate words or lines that read together>"}}],\n'
    ' "missing": ["<visible things the list misses>"]}}\n'
    "Use only ids from the list. At most 12 groups, 25 relations, 10 sentences.")


# ---------------------------------------------------------------- geometry
def area(b) -> float:
    return max(0.0, b[2]) * max(0.0, b[3])


def inter(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def iou(a, b) -> float:
    i = inter(a, b)
    u = area(a) + area(b) - i
    return i / u if u > 0 else 0.0


def contains_pt(b, x, y) -> bool:
    return b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]


def vov(a, b) -> float:
    """Vertical overlap, as a share of the smaller height."""
    o = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, o) / max(1.0, min(a[3], b[3]))


def hov(a, b) -> float:
    o = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    return max(0.0, o) / max(1.0, min(a[2], b[2]))


def fmt_box(b, exact=True) -> str:
    """[x,y w×h]; ≈[...] for a box the eye estimated (not snapped to the pixels)."""
    return f"{'' if exact else '≈'}[{round(b[0])},{round(b[1])} {round(b[2])}×{round(b[3])}]"


def clip(s, n: int = 120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_items(text: str) -> List[dict]:
    """The eye's JSON array of {type,label,box_2d}; tolerant of fences, prose and a truncated tail."""
    if not text:
        return []
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        except ValueError:
            pass
    out = []
    for m in re.finditer(r"\{[^{}]*\}", text):  # object by object (a cut-off answer keeps its complete objects)
        try:
            v = json.loads(m.group(0))
            if isinstance(v, dict):
                out.append(v)
        except ValueError:
            continue
    return out


def parse_obj(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def to_box(b2d, region) -> Optional[list]:
    """box_2d [ymin, xmin, ymax, xmax] (0-1000 of the picture the eye saw) -> [x, y, w, h] in image pixels."""
    if not (isinstance(b2d, list) and len(b2d) == 4):
        return None
    try:
        y1, x1, y2, x2 = [min(1000.0, max(0.0, float(v))) for v in b2d]
    except (TypeError, ValueError):
        return None
    rx, ry, rw, rh = region
    x1, x2 = sorted((rx + x1 / 1000 * rw, rx + x2 / 1000 * rw))
    y1, y2 = sorted((ry + y1 / 1000 * rh, ry + y2 / 1000 * rh))
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return [round(x1), round(y1), round(x2 - x1), round(y2 - y1)]


def norm_label(s) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(s or "").lower().replace("×", "x"))


def norm_type(raw: str, label: str):
    r = str(raw or "").strip().lower().replace(" ", "_")
    t = TYPE_MAP.get(r) or TYPE_MAP.get(r.rstrip("s")) or ("shape" if r in SHAPE_KINDS else "region")
    kind = None
    if t == "shape":
        kind = r if r in SHAPE_KINDS else next((k for k in SHAPE_KINDS if k in str(label).lower()), None)
    return t, kind


# ---------------------------------------------------------------- memory
class Mem:
    """One image's memory: meta, map, notes, the tail of the trace. Writes are serialised per image."""

    def __init__(self, store: "MapStore", sha: str):
        self.store, self.sha = store, sha
        self.dir = store.dir_of(sha)
        self.lock = threading.RLock()
        self.meta = self._load("meta.yaml") or {"sha": sha, "first4": sha[:4]}
        self.map = self._load("map.yaml") or {}
        self.notes = self._load("notes.yaml") or {}
        self.trace = self._trace_tail(400)
        self.status = ""  # mapping progress, not saved
        for name in ("meta.yaml.tmp", "map.yaml.tmp", "notes.yaml.tmp"):  # a save cut short by an exit
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass

    def _load(self, name):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                v = yaml.safe_load(f)
                return v if isinstance(v, dict) else None
        except (OSError, yaml.YAMLError):
            return None

    def _trace_tail(self, n):
        try:
            with open(os.path.join(self.dir, "trace.jsonl"), encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for l in lines:
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
        return out

    def save(self, *names):
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            for name in names or ("meta.yaml", "map.yaml", "notes.yaml"):
                data = {"meta.yaml": self.meta, "map.yaml": self.map, "notes.yaml": self.notes}[name]
                tmp = os.path.join(self.dir, name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=140, default_flow_style=None)
                os.replace(tmp, os.path.join(self.dir, name))

    def add_trace(self, ev: dict):
        ev = dict(ev, ts=ev.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%S"))
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "trace.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.trace.append(ev)
            self.trace = self.trace[-400:]
            t = self.meta.setdefault("trace", {})
            t["events"] = int(t.get("events") or 0) + 1
            t["last"] = ev["ts"]
            self.save("meta.yaml")

    def seen(self, info: dict, count_open: bool):
        """Identity bookkeeping each time the pad shows this content."""
        with self.lock:
            m = self.meta
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            m.setdefault("sha", self.sha)
            m["first4"] = self.sha[:4]
            for k in ("w", "h"):
                if info.get(k):
                    m[k] = info[k]
            if info.get("file"):
                m["file"] = info["file"]  # where the pad reads it now
            m.pop("pad_file", None)  # (the pad kept a copy once)
            name, path = info.get("name"), info.get("path")
            if name and name not in (m.get("names") or []):
                m["names"] = (m.get("names") or []) + [name]
            if path and path not in (m.get("paths") or []):
                m["paths"] = (m.get("paths") or [])[-9:] + [path]
            m.setdefault("first_seen", now)
            m["last_seen"] = now
            if count_open:
                m["opens"] = int(m.get("opens") or 0) + 1
            self.save("meta.yaml")

    def add_qa(self, q, a, by="user"):
        with self.lock:
            qa = self.notes.setdefault("qa", [])
            qa.append({"q": clip(q, 400), "a": clip(a, 900), "by": by, "ts": time.strftime("%Y-%m-%dT%H:%M")})
            self.notes["qa"] = qa[-40:]
            self.meta.setdefault("notes", {})["qa"] = len(self.notes["qa"])
            self.save("notes.yaml", "meta.yaml")

    def add_finding(self, text, ids=None, by="disp"):
        with self.lock:
            f = self.notes.setdefault("findings", [])
            f.append({"text": clip(text, 600), "ids": ids or [], "by": by, "ts": time.strftime("%Y-%m-%dT%H:%M")})
            self.notes["findings"] = f[-60:]
            self.meta.setdefault("notes", {})["findings"] = len(self.notes["findings"])
            self.save("notes.yaml", "meta.yaml")

    # ----- the map
    def elements(self) -> List[dict]:
        return self.map.get("elements") or []

    def element(self, eid: str) -> Optional[dict]:
        eid = str(eid or "").strip()
        base = eid.split(".")[0]
        for e in self.elements():
            if e.get("id") == base:
                if "." in eid:
                    for w in e.get("words") or []:
                        if w.get("id") == eid:
                            return dict(w, type="word", parent=base)
                return e
        if base == "e0" and self.meta.get("w"):
            return {"id": "e0", "type": "image", "label": "the image", "box": [0, 0, self.meta["w"], self.meta["h"]]}
        return None

    def hit(self, x, y) -> Optional[dict]:
        """The deepest (smallest) element under a pixel."""
        best = None
        for e in self.elements():
            b = e.get("box")
            if b and contains_pt(b, x, y) and (best is None or area(b) < area(best["box"])):
                best = e
        return best

    def block(self, bid: str) -> Optional[dict]:
        return next((b for b in self.map.get("blocks") or [] if b.get("id") == bid), None)

    def ref_line(self, rid: str) -> str:
        """One picked component, for the prompt: e5 button "Save" [x,y w×h] / b2 text block "...". """
        rid = str(rid or "").strip()
        if rid.startswith("b"):
            b = self.block(rid)
            return (f"{rid} text block ({b.get('kind')}) \"{clip(b.get('text'), 160)}\" {fmt_box(b['box'], False)} "
                    f"← {' '.join(b.get('ids') or [])}") if b else f"{rid} (not in the map)"
        e = self.element(rid)
        return el_line(e) if e and e.get("type") not in ("word", "image") else (
            f"{rid} {e['type']} \"{e.get('t') or e.get('label')}\" {fmt_box(e['box'], False)}" if e else f"{rid} (not in the map)")

    def inside(self, region) -> List[dict]:
        return [e for e in self.elements() if e.get("box") and inter(e["box"], region) >= 0.8 * area(e["box"])]

    def find(self, query: str, limit=20) -> List[dict]:
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        scored = []
        for e in self.elements():
            hay = f"{e.get('type')} {e.get('kind') or ''} {e.get('label') or ''}".lower()
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, -area(e.get("box") or [0, 0, 0, 0]), e))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [s[2] for s in scored[:limit]]


class MapStore:
    def __init__(self, root: str):
        self.root = root
        self.cache: Dict[str, Mem] = {}
        self.lock = threading.Lock()
        os.makedirs(root, exist_ok=True)
        readme = os.path.join(root, "README.md")
        if not os.path.isfile(readme):
            try:
                with open(readme, "w", encoding="utf-8") as f:
                    f.write(README)
            except OSError:
                pass

    def dir_of(self, sha: str) -> str:
        return os.path.join(self.root, sha[0:1], sha[1:2], sha[2:4], sha)

    def flush(self):
        """Wait for the saves in progress (disp is quitting: its worker threads die with it)."""
        with self.lock:
            mems = list(self.cache.values())
        for m in mems:
            with m.lock:
                pass

    def get(self, sha: str) -> Optional[Mem]:
        if not sha or len(sha) < 8:
            return None
        with self.lock:
            m = self.cache.get(sha)
            if m is None:
                m = self.cache[sha] = Mem(self, sha)
            return m


# ---------------------------------------------------------------- the prefill
class Mapper:
    """Builds an image's map. eye(question, png_b64, max_tokens, name) -> text; pad(op, **kw) -> dict (the pad socket)."""

    def __init__(self, eye: Callable, pad: Callable, log: Callable, detail_passes=3, most=40, workers=2):
        self.eye, self.pad, self.log = eye, pad, log
        self.detail_passes, self.most, self.workers = detail_passes, most, workers

    @staticmethod
    def record_look(mem: Mem, name, ex):
        """What the eye was shown, kept as a recipe (the picture is a region of the image, made again on demand)."""
        with mem.lock:
            looks = [l for l in mem.map.get("looks") or [] if l.get("name") != name]
            looks.append({"name": name, "region": ex.get("region"), "scale": round(float(ex.get("scale") or 1), 3),
                          "size": [ex.get("w"), ex.get("h")]})
            mem.map["looks"] = looks

    def look(self, mem: Mem, n, region, name, where="", max_side=1024) -> List[dict]:
        ex = self.pad("export", image=n, region=region, max=max_side, marks=False)
        self.record_look(mem, name, ex)
        q = INVENTORY_Q.format(where=where, most=self.most)
        text = self.eye(q, ex["png_b64"], 3000, name)
        items = []
        for it in parse_items(text):
            box = to_box(it.get("box_2d") or it.get("box"), ex["region"])
            if not box:
                continue
            label = clip(it.get("label") or it.get("text") or it.get("name") or "", 120)
            t, kind = norm_type(it.get("type"), label)
            items.append({"type": t, "kind": kind, "label": label, "box": box})
        return items

    @staticmethod
    def same(a, b) -> bool:
        """The same element seen twice. The eye's boxes are noisy (tens of pixels), so the same name close by
        counts too: centres within a box width across and a few line heights down."""
        ab, bb = a["box"], b["box"]
        la, lb = norm_label(a.get("label")), norm_label(b.get("label"))
        if iou(ab, bb) >= 0.6:
            return a["type"] == b["type"] or (la and la == lb)
        if la and lb and (la == lb or (min(len(la), len(lb)) >= 3 and (la in lb or lb in la))):
            dx = abs((ab[0] + ab[2] / 2) - (bb[0] + bb[2] / 2))
            dy = abs((ab[1] + ab[3] / 2) - (bb[1] + bb[3] / 2))
            return dx <= max(ab[2], bb[2]) and dy <= 2.5 * max(ab[3], bb[3])
        return False

    @staticmethod
    def dedupe(mem: Mem) -> int:
        """After snapping, two finds of one element sit on the same blob: keep the first (overview) one."""
        els, keep, gone = mem.elements(), [], 0
        for e in els:
            twin = next((k for k in keep if iou(k["box"], e["box"]) >= 0.75 and
                         (k["type"] == e["type"] or norm_label(k.get("label")) == norm_label(e.get("label"))
                          or (k.get("snapped") and e.get("snapped")))), None)
            if twin:
                if e["type"] == "text" and len(e.get("label") or "") > len(twin.get("label") or ""):
                    twin["label"] = e["label"]
                gone += 1
                continue
            keep.append(e)
        mem.map["elements"] = keep
        return gone

    def merge(self, mem: Mem, items, src, replace_box=False) -> int:
        els = mem.map.setdefault("elements", [])
        added = 0
        for it in items:
            hit = next((e for e in els if self.same(e, it)), None)
            if hit:
                if replace_box:  # a zoomed look measures better
                    hit["box"] = it["box"]
                if it["type"] == "text" and len(it["label"]) > len(hit.get("label") or ""):
                    hit["label"] = it["label"]
                continue
            n = int(mem.map.get("next_id") or 1)
            mem.map["next_id"] = n + 1
            e = {"id": f"e{n}", "type": it["type"], "label": it["label"], "box": it["box"], "src": src}
            if it.get("kind"):
                e["kind"] = it["kind"]
            els.append(e)
            added += 1
        return added

    def build(self, mem: Mem, info: dict, cancel: threading.Event, progress: Callable, fresh=False):
        """All stages (or the missing ones); progress(stage_text) after each."""
        n, W, H = info.get("n"), float(info.get("w") or mem.meta.get("w") or 0), float(info.get("h") or mem.meta.get("h") or 0)
        t0 = time.time()
        with mem.lock:
            if fresh or mem.map.get("version") != MAP_VERSION:
                mem.map = {"version": MAP_VERSION, "image": {"sha": mem.sha, "w": int(W), "h": int(H)},
                           "stages": [], "elements": [], "next_id": 1}
        done = set(mem.map.get("stages") or [])

        def stage_done(name, note):
            with mem.lock:
                if name not in mem.map["stages"]:
                    mem.map["stages"].append(name)
                mem.map["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                mem.meta["map"] = {"elements": len(mem.elements()), "relations": len(mem.map.get("relations") or []),
                                   "stages": list(mem.map["stages"]), "updated": mem.map["updated"]}
                mem.save("map.yaml", "meta.yaml")
            progress(f"{name}: {note}")

        def check():
            if cancel.is_set():
                raise InterruptedError("map cancelled")

        # 1 inventory: the whole picture, a flat list
        if "inventory" not in done:
            mem.status = "1/5 inventory: the eye lists what it sees"
            progress(mem.status)
            items = self.look(mem, n, None, "overview")
            check()
            k = self.merge(mem, items, "overview")
            stage_done("inventory", f"{k} elements")
        # 2 detail: zoomed looks into the biggest containers, or tiles of a large picture
        if "detail" not in done:
            check()
            regions = self.detail_regions(mem, W, H)
            mem.status = f"2/5 detail: {len(regions)} zoomed looks"
            progress(mem.status)
            added = 0
            if regions:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futs = [(src, pool.submit(self.look, mem, n, reg, f"detail-{i + 1}",
                                              " (a zoomed part of a larger image; list what is inside it)", 896))
                            for i, (src, reg) in enumerate(regions)]
                    for src, fu in futs:
                        try:
                            items = fu.result()
                        except Exception as e:  # one failed look does not stop the map
                            self.log(f"   detail {src} failed: {type(e).__name__}: {e}")
                            continue
                        reg = dict(regions)[src]
                        items = [it for it in items if iou(it["box"], reg) < 0.8]  # the container itself again
                        if src.startswith("tile"):  # a tile adds the small things the overview could not read
                            items = [it for it in items if area(it["box"]) <= 0.05 * area(reg)]
                        added += self.merge(mem, items, f"detail:{src}", replace_box=True)
                        check()
            stage_done("detail", f"+{added} elements from {len(regions)} zoomed looks")
        # 3 pixels: boxes snapped to the pixels, colours measured, not guessed
        check()
        mem.status = "3/5 pixels: snapping boxes, measuring colours"
        progress(mem.status)
        snapped = 0
        for e in mem.elements():
            check()
            if e.get("snapped") is None:
                snapped += self.snap(e, n, W, H)
            try:
                st = self.pad("stats", image=n, region=e["box"])
            except Exception:
                continue
            dom = [d.get("hex") for d in st.get("dominant") or []][:3]
            c = {"mean": st.get("mean_hex"), "dominant": dom}
            if e["type"] == "text" and len(dom) >= 2:
                c["bg"], c["fg"] = dom[0], dom[1]
            e["color"] = c
        dup = self.dedupe(mem)
        stage_done("pixels", f"{snapped} boxes snapped to the pixels, {dup} duplicates merged, colours measured")
        # 4 structure: tree, words, reading order, text blocks, geometric relations
        check()
        mem.status = "4/5 structure: tree, reading order, text blocks"
        progress(mem.status)
        with mem.lock:
            structure(mem.map, W, H)
        stage_done("structure", f"{len(mem.map.get('blocks') or [])} text blocks, "
                                f"{len([r for r in mem.map.get('relations') or [] if r.get('by') == 'geometry'])} geometric relations")
        # 5 relations: the LLM looks at the picture and the tree together
        check()
        mem.status = "5/5 relations: what goes with what"
        progress(mem.status)
        try:
            self.relations(mem, n, W, H)
            note = (f"{len([r for r in mem.map.get('relations') or [] if r.get('by') == 'llm'])} relations, "
                    f"{len(mem.map.get('groups') or [])} groups")
        except Exception as e:
            note = f"failed: {type(e).__name__}: {e}"
        mem.status = ""
        mem.map["seconds"] = round(time.time() - t0 + float(mem.map.get("seconds") or 0))
        stage_done("relations", note)

    def snap(self, e, n, W, H) -> int:
        """Snap one element's box to the pixels (the pad's snap: the blob of its colour around the eye's box).
        Shapes, boxes, buttons, icons: accepted when the blob overlaps the eye's box and fills its own box; text: only
        when the blob has the line's height and sits under it horizontally. Backgrounds, figures, patterns: kept."""
        t, b = e["type"], e["box"]
        e["snapped"] = False
        if t in ("region", "figure", "pattern") or area(b) > 0.5 * W * H or min(b[2], b[3]) < 3:
            return 0
        seed = b
        for _ in range(2):
            try:
                r = self.pad("snap", image=n, seed=seed, text=t == "text")
            except Exception:
                return 0
            nb, win = r.get("box"), r.get("window") or [0, 0, W, H]
            if not nb or r.get("pixels", 0) < 12 or area(nb) >= 0.85 * area(win):
                return 0
            if not r.get("edge"):
                break
            seed = nb  # the blob runs past the window: look again around it
        if t == "text":
            ok = 0.4 <= nb[3] / max(1, b[3]) <= 2.5 and hov(nb, b) >= 0.5 and nb[2] <= 1.6 * b[2] + 8
        else:
            ok = ((iou(nb, b) >= 0.15 or (inter(nb, b) > 0 and area(nb) <= 4 * area(b))) and r.get("fill", 0) >= 0.3
                  and area(nb) >= 0.2 * area(b))  # not one glyph of a button's label
        if not ok:
            return 0
        e["eye_box"], e["box"], e["snapped"] = b, [int(v) for v in nb], True
        return 1

    def detail_regions(self, mem: Mem, W, H):
        """Where to look closer: the biggest containers (6-70 % of the picture), else tiles of a large picture."""
        if not (W and H):
            return []
        full = W * H
        cands = [e for e in mem.elements() if e["type"] in ("box", "figure", "region")
                 and 0.06 * full <= area(e["box"]) <= 0.7 * full]
        cands.sort(key=lambda e: -area(e["box"]))
        out = []
        for e in cands:
            if any(iou(e["box"], r) > 0.5 for _, r in out):
                continue
            x, y, w, h = e["box"]
            m = 0.04 * max(w, h)
            out.append((e["id"], [max(0, round(x - m)), max(0, round(y - m)), round(min(W, x + w + m) - max(0, x - m)),
                                  round(min(H, y + h + m) - max(0, y - m))]))
            if len(out) >= self.detail_passes:
                break
        if not out and max(W, H) > 1400:  # nothing to zoom into: tiles with 8 % overlap
            cols = 2 if W >= H * 0.75 else 1
            rows = 2 if H >= W * 0.75 else 1
            tw, th = W / cols, H / rows
            ov = 0.08 * max(tw, th)
            for r in range(rows):
                for c in range(cols):
                    x0, y0 = max(0, c * tw - ov), max(0, r * th - ov)
                    out.append((f"tile{r + 1}.{c + 1}", [round(x0), round(y0), round(min(W, (c + 1) * tw + ov) - x0),
                                                         round(min(H, (r + 1) * th + ov) - y0)]))
            out = out[: max(1, self.detail_passes + 1)]
        return out

    def relations(self, mem: Mem, n, W, H):
        ex = self.pad("export", image=n, max=896, marks=False)
        self.record_look(mem, "relations", ex)
        tree = "\n".join(tree_lines(mem.map, W, H, colors=False, max_lines=90))
        blocks = mem.map.get("blocks") or []
        btxt = ("Text blocks found by position:\n" + "\n".join(f"  {b['id']} {b.get('kind')}: \"{clip(b.get('text'), 100)}\" ({', '.join(b.get('ids') or [])})"
                                                           for b in blocks[:20]) + "\n\n") if blocks else ""
        text = self.eye(RELATIONS_Q.format(w=int(W), h=int(H), tree=tree, blocks=btxt), ex["png_b64"], 1800, "relations")
        d = parse_obj(text) or {}
        ids = {e["id"] for e in mem.elements()} | {"e0"}
        with mem.lock:
            m = mem.map
            if d.get("kind"):
                m["kind"] = str(d["kind"])[:40]
            if d.get("summary"):
                m["summary"] = clip(d["summary"], 600)
            m["groups"] = [{"name": clip(g.get("name"), 40), "ids": [i for i in g.get("ids") or [] if i in ids]}
                           for g in d.get("groups") or [] if isinstance(g, dict) and g.get("name")][:12]
            rels = [r for r in m.get("relations") or [] if r.get("by") != "llm"]
            for r in (d.get("relations") or [])[:25]:
                if isinstance(r, dict) and r.get("a") in ids and r.get("b") in ids and r.get("rel"):
                    rels.append({"a": r["a"], "rel": clip(r["rel"], 30), "b": r["b"], "by": "llm"})
            m["relations"] = rels
            m["sentences"] = [{"ids": [i for i in s.get("ids") or [] if i in ids], "text": clip(s.get("text"), 300)}
                              for s in d.get("sentences") or [] if isinstance(s, dict) and s.get("text")][:10]
            m["missing"] = [clip(x, 120) for x in d.get("missing") or [] if isinstance(x, str)][:10]
            if m.get("summary"):
                mem.meta["summary"] = m["summary"]
            if m.get("kind"):
                mem.meta["kind"] = m["kind"]


# ---------------------------------------------------------------- structure (no LLM: boxes and pixels only)
def reading_key(e, unit):
    b = e["box"]
    return (round((b[1] + b[3] / 2) / max(4.0, unit)), b[0])


def structure(m: dict, W, H):
    els = m.get("elements") or []
    full = [0, 0, W, H]
    for e in els:  # parents: the smallest container holding ≥ 90 % of an element, clearly bigger than it
        e.pop("children", None)
        best = None
        for p in els:
            if p is e or p["type"] not in CONTAINERS:
                continue
            if area(p["box"]) < 1.3 * area(e["box"]) or inter(e["box"], p["box"]) < 0.9 * area(e["box"]):
                continue
            if best is None or area(p["box"]) < area(best["box"]):
                best = p
        e["parent"] = best["id"] if best else "e0"
    hs = sorted(e["box"][3] for e in els if e["type"] == "text") or [H * 0.03]
    unit = hs[len(hs) // 2]
    kids: Dict[str, List[dict]] = {}
    for e in els:
        kids.setdefault(e["parent"], []).append(e)
    for pid, ch in kids.items():
        ch.sort(key=lambda e: reading_key(e, unit))
    m["tree"] = {pid: [c["id"] for c in ch] for pid, ch in kids.items()}
    order = []

    def walk(pid):
        for c in kids.get(pid, []):
            order.append(c["id"])
            walk(c["id"])
    walk("e0")
    m["order"] = order
    # words of each text line, boxes estimated by character position
    for e in els:
        if e["type"] != "text":
            e.pop("words", None)
            continue
        text = e.get("label") or ""
        x, y, w, h = e["box"]
        cw = w / max(1, len(text))
        words, k = [], 0
        for mt in re.finditer(r"\S+", text):
            k += 1
            words.append({"id": f"{e['id']}.w{k}", "t": mt.group(0),
                          "box": [round(x + mt.start() * cw), y, max(1, round(len(mt.group(0)) * cw)), h]})
        if len(words) > 1:
            e["words"] = words
        else:
            e.pop("words", None)
    # text blocks: lines of one parent that read together (close below each other, overlapping in x) or side by side
    texts = sorted([e for e in els if e["type"] == "text"], key=lambda e: reading_key(e, unit))
    used, blocks = set(), []
    for t in texts:
        if t["id"] in used:
            continue
        group, last = [t], t
        used.add(t["id"])
        changed = True
        while changed:
            changed = False
            for u in texts:
                if u["id"] in used or u["parent"] != t["parent"]:
                    continue
                lb, ub = last["box"], u["box"]
                gap_y = ub[1] - (lb[1] + lb[3])
                gap_x = ub[0] - (lb[0] + lb[2])
                below = -0.3 * lb[3] <= gap_y <= 0.8 * max(lb[3], ub[3]) and hov(lb, ub) > 0.2
                beside = vov(lb, ub) > 0.6 and 0 <= gap_x <= 1.5 * max(lb[3], ub[3])
                if below or beside:
                    group.append(u)
                    used.add(u["id"])
                    last = u
                    changed = True
        txt = " ".join(g.get("label") or "" for g in group)
        words = len(txt.split())
        kind = ("paragraph" if len(group) > 2 else "sentence") if (words >= 5 or re.search(r"[.!?:]$", txt)) else "label"
        blocks.append({"id": f"b{len(blocks) + 1}", "kind": kind, "ids": [g["id"] for g in group], "text": clip(txt, 400),
                       "box": union([g["box"] for g in group])})
    for b in blocks:  # a label standing alone, far from any other text, says so
        if b["kind"] == "label":
            near = [o for o in blocks if o is not b and dist(o["box"], b["box"]) < 3 * unit]
            if not near:
                b["kind"] = "label, apart"
    m["blocks"] = blocks
    # geometric relations between siblings: rows, left-of (neighbours in a row), above (nearest below), overlaps
    rels = [r for r in m.get("relations") or [] if r.get("by") not in ("geometry",)]
    groups = [g for g in m.get("groups") or [] if not str(g.get("name", "")).startswith("row ")]
    for pid, ch in kids.items():
        rows: List[List[dict]] = []
        for e in sorted(ch, key=lambda e: e["box"][1] + e["box"][3] / 2):
            for r in rows:
                if vov(r[0]["box"], e["box"]) > 0.5:
                    r.append(e)
                    break
            else:
                rows.append([e])
        for r in rows:
            r.sort(key=lambda e: e["box"][0])
            if len(r) >= 3:
                groups.append({"name": f"row in {pid}", "ids": [e["id"] for e in r], "by": "geometry"})
            for a, b in zip(r, r[1:]):
                rels.append({"a": a["id"], "rel": "left-of", "b": b["id"], "by": "geometry"})
        for i, r in enumerate(rows[:-1]):
            nxt = rows[i + 1]
            for a in r:
                below = [b for b in nxt if hov(a["box"], b["box"]) > 0.3]
                if below:
                    rels.append({"a": a["id"], "rel": "above", "b": below[0]["id"], "by": "geometry"})
        for i, a in enumerate(ch):
            for b in ch[i + 1:]:
                o = iou(a["box"], b["box"])
                if 0.1 <= o < 0.6:
                    rels.append({"a": a["id"], "rel": "overlaps", "b": b["id"], "by": "geometry"})
    # a label inside a button/box that holds no other text names it; a label right next to an icon/shape labels it
    for b in blocks:
        first = next((e for e in els if e["id"] == b["ids"][0]), None)
        if not first:
            continue
        par = next((e for e in els if e["id"] == first["parent"]), None)
        if par and par["type"] in ("button", "box") and len([x for x in kids.get(par["id"], []) if x["type"] == "text"]) == 1:
            rels.append({"a": first["id"], "rel": "names", "b": par["id"], "by": "geometry"})
            continue
        for e in kids.get(first["parent"], []):
            if e["type"] in ("icon", "shape", "figure") and vov(e["box"], b["box"]) > 0.5 and dist(e["box"], b["box"]) < 2 * unit:
                rels.append({"a": first["id"], "rel": "labels", "b": e["id"], "by": "geometry"})
                break
    m["relations"] = rels[:300]
    m["groups"] = groups[:40]


def union(boxes):
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[0] + b[2] for b in boxes)
    y2 = max(b[1] + b[3] for b in boxes)
    return [x1, y1, x2 - x1, y2 - y1]


def dist(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]))
    return (dx * dx + dy * dy) ** 0.5


# ---------------------------------------------------------------- views of the map
def el_line(e, colors=True) -> str:
    t = e["type"] + (f":{e['kind']}" if e.get("kind") else "")
    s = f"{e['id']} {t} \"{clip(e.get('label'), 70)}\" {fmt_box(e['box'], e.get('snapped') is True)}"
    c = e.get("color") or {}
    if colors and c:
        if e["type"] == "text" and c.get("fg"):
            s += f" ■{c['fg']} on ■{c.get('bg')}"
        elif c.get("mean"):
            s += f" ■{c['mean']}"
    return s


def tree_lines(m: dict, W, H, colors=True, max_lines=400) -> List[str]:
    els = {e["id"]: e for e in m.get("elements") or []}
    tree = m.get("tree") or {}
    if not tree and els:  # before the structure stage: flat under the image
        tree = {"e0": [e for e in els]}
    out = [f"e0 image {int(W)}×{int(H)}"]

    def walk(pid, prefix):
        ch = tree.get(pid) or []
        for i, cid in enumerate(ch):
            e = els.get(cid)
            if not e:
                continue
            last = i == len(ch) - 1
            out.append(prefix + ("└─ " if last else "├─ ") + el_line(e, colors))
            if len(out) >= max_lines:
                return
            walk(cid, prefix + ("   " if last else "│  "))
    walk("e0", "")
    if len(out) >= max_lines:
        out.append(f"… ({len(els)} elements in all)")
    return out


TRACE_ICON = {"click": "👆", "select": "▭", "mark": "✏", "unmark": "⌫", "ask": "❓", "view": "🔍", "crop": "✂",
              "open": "🖼", "focus": "→", "pick": "⦿", "grid": "▦"}


def trace_line(ev: dict) -> str:
    t = str(ev.get("ts") or "")[5:16].replace("T", " ")
    k = ev.get("ev")
    s = f"{t} {TRACE_ICON.get(k, '·')} {k}"
    if k == "click":
        s += f" ({ev.get('x')}, {ev.get('y')}) ■{ev.get('hex')}"
    elif k in ("select", "view"):
        s += f" {fmt_box(ev.get('region') or ev.get('visible') or [0, 0, 0, 0])}" + (f" zoom {ev.get('zoom')}%" if k == "view" else "")
    elif k == "mark":
        s += f" {ev.get('id')} {ev.get('shape')}" + (f" \"{ev.get('text')}\"" if ev.get("text") else "")
    elif k == "ask":
        s += f" \"{clip(ev.get('text'), 80)}\"" + (f" about {' '.join(ev['refs'])}" if ev.get("refs") else "")
    elif k == "pick":
        s += f" {' '.join(ev.get('added') or ([ev['id']] if ev.get('id') else []))} {'on' if ev.get('on') else 'off'}" \
             f" → {' '.join(ev.get('ids') or []) or 'none'}"
    elif k == "grid":
        s += " on" if ev.get("on") else " off"
    elif k == "crop":
        s += f" → {ev.get('child')}"
    elif k == "open":
        s += f" {ev.get('name') or ''} ({ev.get('source') or ''})"
    if ev.get("hit"):
        s += f" on {ev['hit']}" + (f" \"{clip(ev.get('hit_label'), 40)}\"" if ev.get("hit_label") else "")
    return s


def render(mem: Mem, info: dict, busy: str = "") -> List[str]:
    """The MAP pane of one image: identity, summary, tree, text, relations, the flat inventory, notes, trace."""
    me, m = mem.meta, mem.map
    W, H = info.get("w") or me.get("w") or 0, info.get("h") or me.get("h") or 0
    out = [f"{mem.sha[:4]}#{info.get('n', '?')} {info.get('name') or (me.get('names') or ['?'])[-1]} · {W}×{H}"
           f" · seen {me.get('opens') or 1}× · first {str(me.get('first_seen') or '')[:16].replace('T', ' ')}"
           f" · last {str(me.get('last_seen') or '')[:16].replace('T', ' ')}"]
    for p in (me.get("paths") or [])[-2:]:
        out.append(f"path: {p}")
    if busy:
        out.append(f"⏳ mapping {busy}")
    if me.get("caption"):
        out.append(f"caption: {clip(me['caption'], 300)}")
    if m.get("summary"):
        out.append(f"summary: {m['summary']}")
    if me.get("text"):
        out.append(f"text: {clip(me['text'], 300)}")
    st = m.get("stages") or []
    if m:
        out.append(f"map: {m.get('kind') or '?'} · {len(m.get('elements') or [])} elements · "
                   f"{len(m.get('relations') or [])} relations · stages {' '.join(st) or '-'}"
                   + (f" · {m.get('seconds')}s" if m.get("seconds") else ""))
    elif not busy:
        out.append("map: not built yet (===m maps it, or ask disp to)")
    if m.get("looks"):
        out.append("looks: " + " · ".join(f"{l['name']} {fmt_box(l.get('region') or [0, 0, 0, 0])} ×{l.get('scale')}"
                                         for l in m["looks"]))
    if m.get("elements"):
        out += ["", "TREE (containment, reading order)"]
        out += tree_lines(m, W, H)
    if m.get("blocks"):
        out += ["", "TEXT (blocks in reading order)"]
        for b in m["blocks"]:
            out.append(f"{b['id']} {b['kind']}: \"{clip(b.get('text'), 160)}\" ← {' '.join(b['ids'])}")
    if m.get("sentences"):
        out += ["", "SENTENCES (words that read together)"]
        for s in m["sentences"]:
            out.append(f"\"{s['text']}\" ← {' '.join(s.get('ids') or [])}")
    if m.get("groups"):
        out += ["", "GROUPS"]
        for g in m["groups"]:
            out.append(f"{g['name']}: {' '.join(g.get('ids') or [])}" + (" (geometry)" if g.get("by") == "geometry" else ""))
    rels = m.get("relations") or []
    if rels:
        out += ["", "RELATIONS"]
        sem = [r for r in rels if r.get("by") != "geometry"]
        geo = [r for r in rels if r.get("by") == "geometry"]
        for r in sem + geo[: max(0, 60 - len(sem))]:
            out.append(f"{r['a']} {r['rel']} {r['b']}" + ("" if r.get("by") == "geometry" else f"  ({r.get('by')})"))
        if len(geo) > 60 - len(sem) > 0:
            out.append(f"… {len(geo) - (60 - len(sem))} more geometric relations in map.yaml")
    if m.get("missing"):
        out += ["", "MISSED BY THE INVENTORY (seen in the relations pass)"]
        out += [f"- {x}" for x in m["missing"]]
    if m.get("elements"):
        out += ["", "INVENTORY (flat, as found)"]
        for e in m["elements"]:
            out.append(f"{el_line(e)} · {e.get('src')}" + (" · snapped" if e.get("snapped") else ""))
    notes = mem.notes
    if notes.get("findings") or notes.get("qa"):
        out += ["", "NOTES"]
        for f in (notes.get("findings") or [])[-10:]:
            out.append(f"K {str(f.get('ts'))[5:16].replace('T', ' ')} {f.get('text')}" + (f" ({' '.join(f['ids'])})" if f.get("ids") else ""))
        for q in (notes.get("qa") or [])[-10:]:
            out.append(f"Q {str(q.get('ts'))[5:16].replace('T', ' ')} {clip(q.get('q'), 100)} → {clip(q.get('a'), 160)}")
    if mem.trace:
        total = (me.get("trace") or {}).get("events") or len(mem.trace)
        out += ["", f"TRACE (what you did on the pad; latest {min(20, len(mem.trace))} of {total})"]
        out += [trace_line(ev) for ev in mem.trace[-20:]]
    return out


def layer(mem: Mem):
    """The map as the pad's grid layer: a dot per element (box centre) and per text block (left middle of its box, so
    it does not sit on its line's dot), links for the relations, the lines of each block and containment."""
    nodes, links = [], []
    for e in mem.elements():
        x, y, w, h = e["box"]
        nodes.append({"id": e["id"], "type": e["type"], "label": clip(e.get("label"), 80), "x": x + w / 2, "y": y + h / 2,
                      "box": [x, y, w, h], "exact": e.get("snapped") is True})
    for b in mem.map.get("blocks") or []:
        bx = b.get("box")
        if not bx:
            continue
        nodes.append({"id": b["id"], "type": "block", "label": clip(b.get("text"), 80), "x": bx[0], "y": bx[1] + bx[3] / 2,
                      "box": bx, "exact": False})
        links += [{"a": b["id"], "b": i, "rel": "line of", "kind": "block"} for i in b.get("ids") or []]
    ids = {n["id"] for n in nodes}
    for r in mem.map.get("relations") or []:
        if r.get("a") in ids and r.get("b") in ids:
            links.append({"a": r["a"], "b": r["b"], "rel": r.get("rel") or "",
                          "kind": "geo" if r.get("by") == "geometry" else "sem"})
    for e in mem.elements():
        if e.get("parent") in ids:
            links.append({"a": e["parent"], "b": e["id"], "rel": "contains", "kind": "tree"})
    return nodes, links


def compact(mem: Mem, info: dict, budget=3200) -> str:
    """The map for the LLM's context: summary, tree (cut), text blocks, semantic relations."""
    m = mem.map
    if not m.get("elements"):
        return "(no map yet: map_image builds it)"
    W, H = info.get("w") or mem.meta.get("w") or 0, info.get("h") or mem.meta.get("h") or 0
    parts = []
    if m.get("summary"):
        parts.append(f"summary: {m['summary']}")
    parts.append(f"{len(m['elements'])} elements, stages {' '.join(m.get('stages') or [])}")
    tl = tree_lines(m, W, H, colors=True, max_lines=60)
    parts.append("\n".join(tl))
    if m.get("blocks"):
        parts.append("text blocks: " + " | ".join(f"{b['id']} \"{clip(b.get('text'), 80)}\"" for b in m["blocks"][:15]))
    sem = [r for r in m.get("relations") or [] if r.get("by") != "geometry"][:15]
    if sem:
        parts.append("relations: " + "; ".join(f"{r['a']} {r['rel']} {r['b']}" for r in sem))
    if m.get("groups"):
        parts.append("groups: " + "; ".join(f"{g['name']}: {' '.join(g['ids'][:8])}" for g in m["groups"][:8]
                                            if g.get("by") != "geometry"))
    s = "\n".join(parts)
    return s if len(s) <= budget else s[: budget - 40] + "\n… (map_element / map_find for more)"


def element_detail(mem: Mem, eid: str) -> str:
    e = mem.element(eid)
    if not e:
        return f"Error: no element {eid} in the map"
    lines = [el_line(e) if e.get("type") not in ("word", "image") else f"{e['id']} {e['type']} \"{e.get('t') or e.get('label')}\" {fmt_box(e['box'])}"]
    if e.get("parent"):
        p = mem.element(e["parent"])
        lines.append(f"inside: {el_line(p, False) if p and p.get('type') != 'image' else 'e0 the image'}")
    ch = (mem.map.get("tree") or {}).get(e["id"]) or []
    if ch:
        lines.append("contains: " + "; ".join(el_line(mem.element(c), False) for c in ch[:20] if mem.element(c)))
    if e.get("words"):
        lines.append("words: " + " ".join(f"{w['id']}=\"{w['t']}\"{fmt_box(w['box'])}" for w in e["words"][:30]))
    c = e.get("color") or {}
    if c:
        lines.append(f"colours: mean {c.get('mean')} dominant {', '.join(c.get('dominant') or [])}"
                     + (f" · text {c.get('fg')} on {c.get('bg')}" if c.get("fg") else ""))
    rels = [r for r in mem.map.get("relations") or [] if e["id"] in (r.get("a"), r.get("b"))]
    if rels:
        lines.append("relations: " + "; ".join(f"{r['a']} {r['rel']} {r['b']}" for r in rels[:20]))
    blocks = [b for b in mem.map.get("blocks") or [] if e["id"] in b.get("ids", [])]
    for b in blocks:
        lines.append(f"text block {b['id']} ({b['kind']}): \"{clip(b.get('text'), 200)}\"")
    touches = [t for t in mem.trace if t.get("hit") == e["id"]]
    if touches:
        lines.append(f"the user touched it {len(touches)}× (last {touches[-1].get('ts')})")
    return "\n".join(lines)
