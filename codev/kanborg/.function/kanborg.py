#!/usr/bin/env python3
"""
kanborg core: one model of the board for the TUI (.function/kanborg.lua) and the command line.
The CLI (text, or --json) is the stable interface for anything outside the tool.

Stdlib only. The board stays what it always was: markdown cards in datum/<CYCLE>/<STATUS>/<CODE>-<N>.md,
projects in datum/<CYCLE>/PROJECT.md, git as the history. A card's status is its directory; everything
else lives in the one-line JSON footer of the card (legacy keys kept, new keys added on demand):

    { "project_code": "VIM", "links": "", "location": "", "fpoint": "3",          <- legacy
      "depends_on": ["HIPG-1"], "relates": ["GGPU-0"], "parent": "VIM-0",         <- links
      "owner": "bee", "start": "2026-11-01", "due": "2026-11-20", "progress": 40,  <- planning
      "tags": ["infra"] }

CODE-0 is the project's root card (the legacy layout opens every project with it): it carries the
project-level span (start/due) and every other card of the project hangs under it unless "parent" says
otherwise. fpoint, when numeric, weights a card in its project's progress.

CLI (text by default, --json for machines):
    kanborg.py [--root DIR] [--cycle YEAR] [--json] <command> ...
      ids | projects | list [-p CODE..] [-s STATUS] | show CARD
      new CODE "title" [-s STATUS] [-d "project description"] [-k KIND]
      move CARD STATUS | set CARD key=value.. | link CARD depends|relates|parent OTHER | unlink CARD [type] OTHER
      journal CARD "text" | section CARD "NAME" "text" [--append] | project CODE key=value..
      overview | board [CODE..] | lanes [CODE..] | related CODE | progress [CODE..] | deps [CODE..]
      timeline [CODE..] [--from DATE] [--days N] | validate
      search [TERM..] [-p CODE..] [-s STATUS] [--in SECTION|title|meta] [--owner WHO] [--tag TAG]
             [-r] [--any] [-m cards|lines|fields|count] [-n LIMIT]
      history [-m log|moves|diff|show|pending|work] [-c CARD..] [-p CODE..] [--since WHEN] [--until WHEN]
              [--author WHO] [--grep TEXT] [--text REGEX] [--at REV|DATE] [--in SECTION] [--repo DIR..] [-n LIMIT]
              (git, read-only: commits and what they did to cards, status moves and days in each status,
               changed lines and fields, a card as it was, uncommitted changes, work commits named after a card)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict

STATUSES = ["BACKLOG", "TODO", "HOLD", "WIP", "DONE"]
CARD_RE = re.compile(r"\b([A-Z][A-Z0-9]*)-(\d+)\b")
ROW_RE = re.compile(r"^([A-Z][A-Z0-9]*)\s*:\s*(\{.*\})\s*$")
SECTION_RE = re.compile(r"^###\s+(?:\d+\s+)?(.+?)\s*$")
LINK_TYPES = {"depends": "depends_on", "depends_on": "depends_on", "dep": "depends_on", "blocked_by": "depends_on",
              "relates": "relates", "related": "relates", "rel": "relates", "parent": "parent"}
LIST_KEYS = ("depends_on", "relates", "tags")
DEFAULT_PROGRESS = {"BACKLOG": 0, "TODO": 0, "HOLD": 0, "WIP": 50, "DONE": 100}
SETTABLE = ("title", "kind", "owner", "start", "due", "progress", "fpoint", "tags", "location", "links", "parent")


class KbError(Exception):
    pass


# ---------------------------------------------------------------- dates
def today() -> dt.date:
    env = os.environ.get("KANBORG_TODAY")
    return dt.date.fromisoformat(env) if env else dt.date.today()


def parse_date(text, base=None):
    """YYYY-MM-DD | MM-DD | today | +3d | +2w | -1d  -> ISO string ('' clears)."""
    s = str(text or "").strip().lower()
    if s in ("", "none", "-", "null"):
        return ""
    base = base or today()
    if s == "today":
        return base.isoformat()
    m = re.fullmatch(r"([+-]\d+)([dw])", s)
    if m:
        n = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
        return (base + dt.timedelta(days=n)).isoformat()
    if re.fullmatch(r"\d{1,2}-\d{1,2}", s):
        s = f"{base.year}-{s}"
    try:
        y, mo, d = (int(x) for x in s.split("-"))
        return dt.date(y, mo, d).isoformat()
    except (ValueError, TypeError):
        raise KbError(f"bad date '{text}' (use YYYY-MM-DD, MM-DD, today, +3d, +2w)")


def as_date(s):
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- card
def card_key(cid):
    m = CARD_RE.fullmatch(cid or "")
    return (m.group(1), int(m.group(2))) if m else (cid, 0)


class Card:
    def __init__(self, path, status, text=None):
        """text: the card's content when it is not read from path (a version out of git history)."""
        self.path, self.status = path, status
        base = os.path.basename(path)[:-3]
        self.archived = base.startswith("archived.")
        self.id = base[len("archived."):] if self.archived else base
        self.project, self.num = card_key(self.id)
        if text is None:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        self.lines = text.split("\n")
        self.meta, self.footer_at = self._parse_footer()

    # ----- parsing
    def _parse_footer(self):
        for i in range(len(self.lines) - 1, -1, -1):
            s = self.lines[i].strip()
            if s.startswith("{") and s.endswith("}"):
                j = i - 1
                while j >= 0 and not self.lines[j].strip():
                    j -= 1
                if j >= 0 and self.lines[j].strip().startswith("```json"):
                    try:
                        return json.loads(s, object_pairs_hook=OrderedDict), i
                    except ValueError:
                        return OrderedDict(), i
        return OrderedDict(), None

    @property
    def heading(self):
        for line in self.lines[:20]:
            if re.match(r"^#\s+", line):
                return re.sub(r"^#\s+", "", line).rstrip()
        return ""

    @property
    def kind(self):
        m = re.match(r"^([A-Za-z][\w ]*?)\s*:\s*(.*)$", self.heading)
        return m.group(1).strip() if m else ""

    @property
    def title(self):
        m = re.match(r"^([A-Za-z][\w ]*?)\s*:\s*(.*)$", self.heading)
        return (m.group(2) if m else self.heading).strip()

    def get(self, key, default=""):
        v = self.meta.get(key, default)
        return default if v is None else v

    def lst(self, key):
        v = self.meta.get(key)
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
        return []

    @property
    def depends_on(self):
        return self.lst("depends_on")

    @property
    def relates(self):
        return self.lst("relates")

    @property
    def tags(self):
        return self.lst("tags")

    @property
    def parent(self):
        p = str(self.get("parent")).strip()
        if p:
            return p
        return f"{self.project}-0" if self.num > 0 else ""

    @property
    def start(self):
        return as_date(self.get("start"))

    @property
    def due(self):
        return as_date(self.get("due"))

    def checkboxes(self):
        done = total = 0
        for line in self.lines:
            m = re.match(r"^\s*[-*]\s+\[([ xX])\]", line)
            if m:
                total += 1
                done += m.group(1) != " "
        return done, total

    @property
    def progress(self):
        if self.status == "DONE":
            return 100
        p = self.meta.get("progress")
        if p not in (None, ""):
            try:
                return max(0, min(100, int(float(p))))
            except ValueError:
                pass
        done, total = self.checkboxes()
        if total:
            return round(100 * done / total)
        return DEFAULT_PROGRESS.get(self.status, 0)

    @property
    def weight(self):
        try:
            w = float(self.get("fpoint"))
            return w if w > 0 else 1.0
        except (TypeError, ValueError):
            return 1.0

    @property
    def overdue(self):
        return bool(self.due and self.status != "DONE" and self.due < today())

    # ----- editing
    def set_heading_text(self, text):
        """Replace the whole heading text after '# ' (kind prefix included)."""
        text = "# " + " ".join(str(text).split())
        for i, line in enumerate(self.lines[:20]):
            if re.match(r"^#\s+", line):
                self.lines[i] = text
                return
        self.lines.insert(0, text)
        if self.footer_at is not None:
            self.footer_at += 1

    def set_heading(self, kind=None, title=None):
        kind = self.kind if kind is None else kind.strip()
        title = self.title if title is None else title.strip()
        text = f"# {kind} : {title}".rstrip() if kind else f"# {title}"
        for i, line in enumerate(self.lines[:20]):
            if re.match(r"^#\s+", line):
                self.lines[i] = text
                return
        self.lines.insert(0, text)
        if self.footer_at is not None:
            self.footer_at += 1

    def write_meta(self):
        text = json.dumps(self.meta, ensure_ascii=False)
        if self.footer_at is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines += ["", "--------------------------------", "```json", text, "```"]
            self.footer_at = len(self.lines) - 2
        else:
            self.lines[self.footer_at] = text

    def sections(self):
        """[(name, header line index)] of ### sections above the footer."""
        out = []
        end = self.footer_at if self.footer_at is not None else len(self.lines)
        for i, line in enumerate(self.lines[:end]):
            m = SECTION_RE.match(line)
            if m:
                out.append((m.group(1).strip(), i))
        return out

    def _section_bounds(self, name):
        secs = self.sections()
        want = name.strip().lower()
        for k, (n, i) in enumerate(secs):
            if n.lower() == want or n.lower().startswith(want):
                stop = secs[k + 1][1] if k + 1 < len(secs) else self._body_end()
                return i, stop
        raise KbError(f"{self.id} has no section '{name}' (has: {', '.join(n for n, _ in secs) or 'none'})")

    def _body_end(self):
        """Index of the dashed separator before the footer (or the footer fence / end)."""
        if self.footer_at is None:
            end = len(self.lines)
            while end > 0 and not self.lines[end - 1].strip():
                end -= 1
            return end
        for j in range(self.footer_at - 1, -1, -1):
            if self.lines[j].startswith("-----"):
                return j
        return max(0, self.footer_at - 1)

    def section_text(self, name):
        i, stop = self._section_bounds(name)
        return "\n".join(self.lines[i + 1:stop]).strip("\n")

    def set_section(self, name, text, append=False):
        i, stop = self._section_bounds(name)
        body = [l for l in self.lines[i + 1:stop]]
        while body and not body[-1].strip():
            body.pop()
        new = (body if append else []) + str(text).rstrip("\n").split("\n")
        while new and not new[0].strip() and not append:
            new.pop(0)
        block = new + ["", ""]
        self.lines[i + 1:stop] = block
        if self.footer_at is not None:
            self.footer_at += len(block) - (stop - i - 1)

    def save(self):
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(self.lines))

    def summary(self):
        return OrderedDict([
            ("id", self.id), ("project", self.project), ("status", self.status), ("kind", self.kind),
            ("title", self.title), ("heading", self.heading), ("path", self.path), ("owner", self.get("owner")),
            ("start", self.get("start")), ("due", self.get("due")), ("progress", self.progress),
            ("fpoint", self.get("fpoint")), ("depends_on", self.depends_on), ("relates", self.relates),
            ("parent", self.parent), ("tags", self.tags), ("overdue", self.overdue)])


# ---------------------------------------------------------------- board
def read_varset(root):
    out = {}
    path = os.path.join(root, ".properd", "VARSET")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^\s*([A-Z_]+)=(.*)$", line.strip())
            if m and "$(" not in m.group(2):
                out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


class Board:
    def __init__(self, root=".", cycle=None):
        self.root = os.path.abspath(root)
        var = read_varset(self.root)
        self.data_dir = os.environ.get("DATA_DIR") or var.get("DATA_DIR") or "datum"
        self.data_dir = self.data_dir[2:] if self.data_dir.startswith("./") else self.data_dir
        self.cycle = str(cycle or os.environ.get("CYCLE") or today().year)
        self.cycle_dir = os.path.join(self.root, self.data_dir, self.cycle)
        self.project_md = os.path.join(self.cycle_dir, "PROJECT.md")
        self.reload()

    def reload(self):
        self.cards = OrderedDict()
        for st in STATUSES:
            d = os.path.join(self.cycle_dir, st)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".md"):
                    c = Card(os.path.join(d, name), st)
                    self.cards[c.id] = c
        self.cards = OrderedDict(sorted(self.cards.items(), key=lambda kv: card_key(kv[0])))
        self.projects = self._read_projects()

    # ----- projects
    def _read_projects(self):
        out = OrderedDict()
        if os.path.exists(self.project_md):
            for line in open(self.project_md, encoding="utf-8"):
                m = ROW_RE.match(line.strip())
                if m:
                    try:
                        out[m.group(1)] = json.loads(m.group(2), object_pairs_hook=OrderedDict)
                    except ValueError:
                        out[m.group(1)] = OrderedDict(description="(unreadable row)")
        for c in self.cards.values():  # cards of projects missing from PROJECT.md still count
            out.setdefault(c.project, OrderedDict(description="", last_id=c.num, related=""))
        return out

    def _write_projects(self):
        rows = [f"{code} : {json.dumps(p, ensure_ascii=False, separators=(',', ':'))}"
                for code, p in self.projects.items()]
        os.makedirs(self.cycle_dir, exist_ok=True)
        with open(self.project_md, "w", encoding="utf-8", newline="\n") as f:
            f.write("```yaml\n" + "\n".join(rows) + "\n```\n")
        self.git("add", self.project_md)

    def related_projects(self, code):
        """Declared (PROJECT.md "related", both directions) + implied by card links across projects."""
        rel = set()
        p = self.projects.get(code, {})
        rel.update(x for x in re.split(r"[,\s]+", str(p.get("related") or "")) if x)
        for other, op in self.projects.items():
            if code in re.split(r"[,\s]+", str(op.get("related") or "")):
                rel.add(other)
        for c in self.cards.values():
            for ref in c.depends_on + c.relates:
                rp = card_key(ref)[0]
                if c.project == code and rp != code:
                    rel.add(rp)
                elif rp == code and c.project != code:
                    rel.add(c.project)
        rel.discard(code)
        return sorted(rel)

    # ----- lookup
    def card(self, ref):
        ref = str(ref or "").strip()
        m = CARD_RE.search(ref)
        cid = f"{m.group(1)}-{m.group(2)}" if m else ref
        if cid not in self.cards:
            raise KbError(f"no card {cid} in cycle {self.cycle}")
        return self.cards[cid]

    def project_cards(self, code):
        return [c for c in self.cards.values() if c.project == code]

    def blocks(self, cid):
        return [c.id for c in self.cards.values() if cid in c.depends_on]

    def open_deps(self, card):
        return [d for d in card.depends_on if d not in self.cards or self.cards[d].status != "DONE"]

    def blocked(self, card):
        return card.status != "DONE" and bool(self.open_deps(card))

    def _reaches(self, start, target, key):
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen or cur not in self.cards:
                continue
            seen.add(cur)
            c = self.cards[cur]
            stack.extend(c.depends_on if key == "depends_on" else [c.parent] if c.parent else [])
        return False

    def cycles(self):
        out, state = [], {}

        def visit(cid, path):
            state[cid] = 1
            for d in self.cards[cid].depends_on:
                if d not in self.cards:
                    continue
                if state.get(d) == 1:
                    out.append(path[path.index(d):] + [d])
                elif not state.get(d):
                    visit(d, path + [d])
            state[cid] = 2

        for cid in self.cards:
            if not state.get(cid):
                visit(cid, [cid])
        return out

    # ----- git
    def git(self, *args):
        try:
            r = subprocess.run(["git", *args], cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # ----- mutations
    def new_card(self, code, title="", status="BACKLOG", description="", kind=""):
        code = code.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", code):
            raise KbError(f"bad project code '{code}' (letters/digits, starting with a letter)")
        status = self._status(status)
        proj = self.projects.get(code)
        if proj is None or not os.path.exists(self.project_md) or not self._row_exists(code):
            proj = OrderedDict(description=description or (proj or {}).get("description", ""),
                               last_id=(proj or {}).get("last_id", -1), related=(proj or {}).get("related", ""))
            self.projects[code] = proj
        last = int(proj.get("last_id", -1))
        existing = [c.num for c in self.project_cards(code)]
        num = max([last] + existing) + 1
        proj["last_id"] = num
        self._write_projects()
        cid = f"{code}-{num}"
        tpl = os.path.join(self.root, ".template", f"{code}.CARD.md")
        if not os.path.exists(tpl):
            tpl = os.path.join(self.root, "CARD.md")
        text = open(tpl, encoding="utf-8").read() if os.path.exists(tpl) else "# ISSUE :\n"
        for k, v in {"PROJECT_CODE": code, "LINKS": "", "LOCATION": "", "FPOINT": ""}.items():
            text = text.replace("${" + k + "}", v)
        path = os.path.join(self.cycle_dir, status, cid + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        card = Card(path, status)
        self.cards[cid] = card
        if title or kind:
            card.set_heading(kind=kind or None, title=title or None)
        if num == 0 and not card.meta.get("start"):
            card.meta["start"] = today().isoformat()
        card.write_meta()
        card.save()
        self.git("add", path)
        return card

    def _row_exists(self, code):
        return any(ROW_RE.match(l.strip()) and l.strip().startswith(code + " ")
                   for l in open(self.project_md, encoding="utf-8"))

    def _status(self, s):
        s = str(s or "").strip().upper()
        aliases = {"B": "BACKLOG", "T": "TODO", "H": "HOLD", "W": "WIP", "D": "DONE", "INPROGRESS": "WIP"}
        s = aliases.get(s, s)
        if s not in STATUSES:
            raise KbError(f"bad status '{s}' (one of {', '.join(STATUSES)})")
        return s

    def move(self, ref, status):
        card = self.card(ref)
        status = self._status(status)
        if status == card.status:
            return card, []
        dest_dir = os.path.join(self.cycle_dir, status)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(card.path))
        if os.path.exists(dest):
            raise KbError(f"{dest} already exists")
        if not self.git("mv", card.path, dest):
            os.rename(card.path, dest)
            self.git("add", "-A", card.path, dest)
        card.path, card.status = dest, status
        warnings = []
        if status in ("WIP", "DONE") and self.open_deps(card):
            warnings.append(f"{card.id} still waits on {', '.join(self.open_deps(card))}")
        if status == "DONE":
            freed = [b for b in self.blocks(card.id) if not self.open_deps(self.cards[b])]
            if freed:
                warnings.append(f"unblocked: {', '.join(freed)}")
        return card, warnings

    def set_fields(self, ref, pairs):
        card = self.card(ref)
        changed = []
        for key, val in pairs:
            key = key.strip().lower()
            if key not in SETTABLE:
                raise KbError(f"cannot set '{key}' (settable: {', '.join(SETTABLE)})")
            if key == "title":  # the whole heading after "# ", e.g. "FEATURE : timeline view"
                if not str(val).strip():
                    raise KbError("title cannot be empty")
                card.set_heading_text(val)
            elif key == "kind":
                card.set_heading(kind=val.upper())
            elif key in ("start", "due"):
                card.meta[key] = parse_date(val)
            elif key == "progress":
                if str(val).strip() in ("", "auto"):
                    card.meta.pop("progress", None)
                else:
                    try:
                        p = int(str(val).strip().rstrip("%"))
                    except ValueError:
                        raise KbError(f"progress must be 0-100 or 'auto', got '{val}'")
                    if not 0 <= p <= 100:
                        raise KbError("progress must be 0-100")
                    card.meta[key] = p
            elif key == "tags":
                card.meta[key] = [t for t in re.split(r"[,\s]+", val) if t]
            elif key == "parent":
                self._check_link(card, "parent", val) if val else None
                card.meta[key] = val.strip()
            else:
                card.meta[key] = val
            changed.append(key)
        if card.start and card.due and card.due < card.start:
            raise KbError(f"{card.id}: due {card.due} is before start {card.start}")
        card.write_meta()
        card.save()
        self.git("add", card.path)
        return card, changed

    def _check_link(self, card, key, other):
        other = self.card(other).id
        if other == card.id:
            raise KbError("a card cannot link to itself")
        if key in ("depends_on", "parent") and self._reaches(other, card.id, key):
            raise KbError(f"linking {card.id} → {other} would create a cycle")
        return other

    def link(self, ref, ltype, other):
        card = self.card(ref)
        key = LINK_TYPES.get(str(ltype).lower())
        if not key:
            raise KbError(f"bad link type '{ltype}' (depends | relates | parent)")
        other = self._check_link(card, key, other)
        if key == "parent":
            card.meta["parent"] = other
        else:
            cur = card.lst(key)
            if other not in cur:
                cur.append(other)
            card.meta[key] = cur
        card.write_meta()
        card.save()
        self.git("add", card.path)
        return card, key, other

    def unlink(self, ref, other, ltype=None):
        card = self.card(ref)
        keys = [LINK_TYPES[ltype.lower()]] if ltype else ["depends_on", "relates", "parent"]
        removed = []
        for key in keys:
            if key == "parent":
                if str(card.get("parent")) == other:
                    card.meta.pop("parent", None)
                    removed.append(key)
            else:
                cur = card.lst(key)
                if other in cur:
                    cur.remove(other)
                    card.meta[key] = cur
                    removed.append(key)
        if not removed:
            raise KbError(f"{card.id} has no link to {other}")
        card.write_meta()
        card.save()
        self.git("add", card.path)
        return card, removed

    def journal(self, ref, text, who=""):
        card = self.card(ref)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- {stamp}{' @' + who if who else ''}: {text.strip()}"
        names = [n.lower() for n, _ in card.sections()]
        target = next((n for n in ("journal", "discussion", "notes") if any(x.startswith(n) for x in names)), None)
        if target:
            card.set_section(target, entry, append=True)
        else:
            card.lines.insert(card._body_end(), entry)
            if card.footer_at is not None:
                card.footer_at += 1
        card.save()
        self.git("add", card.path)
        return card, entry

    def set_section(self, ref, name, text, append=False):
        card = self.card(ref)
        card.set_section(name, text, append=append)
        card.save()
        self.git("add", card.path)
        return card

    def set_project(self, code, pairs):
        code = code.upper()
        if code not in self.projects:
            raise KbError(f"no project {code}")
        p = self.projects[code]
        for key, val in pairs:
            if key not in ("description", "related"):
                raise KbError("project fields: description, related")
            if key == "related":
                codes = [x.upper() for x in re.split(r"[,\s]+", val) if x]
                bad = [x for x in codes if x not in self.projects or x == code]
                if bad:
                    raise KbError(f"unknown/self project(s): {', '.join(bad)}")
                val = ",".join(codes)
            p[key] = val
        self._write_projects()
        return p

    # ----- metrics
    def project_stats(self, code):
        cards = self.project_cards(code)
        root = self.cards.get(f"{code}-0")
        work = [c for c in cards if c.num != 0] or cards
        total_w = sum(c.weight for c in work) or 1
        progress = round(sum(c.progress * c.weight for c in work) / total_w) if work else 0
        counts = OrderedDict((s, sum(c.status == s for c in cards)) for s in STATUSES)
        starts = [c.start for c in cards if c.start]
        dues = [c.due for c in cards if c.due]
        start = (root.start if root and root.start else min(starts) if starts else None)
        due = (root.due if root and root.due else max(dues) if dues else None)
        nxt = sorted((c.due, c.id) for c in cards if c.due and c.status != "DONE")
        return OrderedDict([
            ("code", code), ("description", self.projects.get(code, {}).get("description", "")),
            ("progress", progress), ("counts", counts), ("cards", len(cards)),
            ("blocked", [c.id for c in cards if self.blocked(c)]),
            ("overdue", [c.id for c in cards if c.overdue]),
            ("start", start.isoformat() if start else ""), ("due", due.isoformat() if due else ""),
            ("next_due", f"{nxt[0][1]} {nxt[0][0].isoformat()}" if nxt else ""),
            ("related", self.related_projects(code))])


# ---------------------------------------------------------------- rendering
def bar(pct, width=10):
    full = round(pct * width / 100)
    return "█" * full + "░" * (width - full)


def dwidth(s):
    """Display width (emoji / East Asian wide chars take two cells)."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1 for ch in s)


def pad(s, n):
    return s + " " * max(0, n - dwidth(s))


def clip(s, n):
    s = " ".join(str(s or "").split())
    if dwidth(s) <= n:
        return s
    while s and dwidth(s) > n - 1:
        s = s[:-1]
    return s + "…"


def badges(b, c):
    out = []
    if b.blocked(c):
        out.append("⛔" + ",".join(b.open_deps(c)))
    if c.overdue:
        out.append("⏰" + c.due.strftime("%m-%d"))
    elif c.due and c.status != "DONE":
        out.append("due " + c.due.strftime("%m-%d"))
    if c.status not in ("DONE", "BACKLOG") and (c.meta.get("progress") not in (None, "") or c.checkboxes()[1]):
        out.append(f"{c.progress}%")
    if c.get("owner"):
        out.append("@" + str(c.get("owner")))
    return " ".join(out)


def pick_projects(b, codes):
    codes = [x.upper() for x in codes or []]
    bad = [x for x in codes if x not in b.projects]
    if bad:
        raise KbError(f"unknown project(s): {', '.join(bad)} (have: {', '.join(b.projects)})")
    return codes or list(b.projects)


def render_overview(b):
    rows = [f"PORTFOLIO  cycle {b.cycle}  ·  {len(b.projects)} projects  ·  {len(b.cards)} cards  ·  today {today()}",
            "",
            f"{'PROJECT':<8} {'PROGRESS':<16} {'B':>2} {'T':>2} {'H':>2} {'W':>2} {'D':>2}  {'SPAN':<23} {'RISK':<14} DESCRIPTION / RELATED"]
    for code in b.projects:
        s = b.project_stats(code)
        n = s["counts"]
        span = f"{s['start'] or '?'}→{s['due'] or '?'}" if s["start"] or s["due"] else ""
        risk = " ".join(x for x in [f"⛔{len(s['blocked'])}" if s["blocked"] else "",
                                    f"⏰{len(s['overdue'])}" if s["overdue"] else ""] if x)
        rel = f"  ↔ {','.join(s['related'])}" if s["related"] else ""
        rows.append(f"{code:<8} {bar(s['progress'])} {s['progress']:>3}%  {n['BACKLOG']:>2} {n['TODO']:>2} "
                    f"{n['HOLD']:>2} {n['WIP']:>2} {n['DONE']:>2}  {span:<23} {risk:<14} "
                    f"{clip(s['description'], 40)}{rel}")
    rows += ["", "B/T/H/W/D = BACKLOG TODO HOLD WIP DONE   ⛔ blocked   ⏰ overdue   ↔ related projects"]
    return "\n".join(rows)


def render_board(b, codes, width=30):
    codes = pick_projects(b, codes)
    cols = {s: [] for s in STATUSES}
    for c in b.cards.values():
        if c.project in codes:
            cell = f"{c.id} {clip(c.title, 14)}"
            bd = badges(b, c)
            cols[c.status].append(clip(cell + (" " + bd if bd else ""), width))
    head = f"BOARD  {', '.join(codes)}  ·  cycle {b.cycle}"
    lines = [head, "", " │ ".join(f"{s:<{width}}" for s in STATUSES),
             "─┼─".join("─" * width for _ in STATUSES)]
    for r in range(max([len(v) for v in cols.values()] + [0])):
        lines.append(" │ ".join(pad(cols[s][r] if r < len(cols[s]) else "", width) for s in STATUSES))
    return "\n".join(lines)


def render_lanes(b, codes, title="LANES"):
    codes = pick_projects(b, codes)
    out = [f"{title}  {', '.join(codes)}  ·  cycle {b.cycle}", ""]
    order = ["WIP", "HOLD", "TODO", "BACKLOG", "DONE"]
    for code in codes:
        s = b.project_stats(code)
        risk = (f" · ⛔ {len(s['blocked'])} blocked" if s["blocked"] else "") + \
               (f" · ⏰ {len(s['overdue'])} overdue" if s["overdue"] else "")
        span = f" · {s['start'] or '?'} → {s['due'] or '?'}" if s["start"] or s["due"] else ""
        out.append(f"== {code} · {clip(s['description'], 40)} · {bar(s['progress'])} {s['progress']}%{span}{risk}")
        cards = sorted(b.project_cards(code), key=lambda c: (order.index(c.status), c.num))
        for c in cards:
            tree = "  " if c.num == 0 or c.parent == f"{code}-0" else "    └ "
            root = "◆ " if c.num == 0 else ""
            out.append(f"{tree}{c.id:<8} {c.status:<7} {root}{clip(c.title or '(untitled)', 40):<42} {badges(b, c)}".rstrip())
        out.append("")
    return "\n".join(out).rstrip()


def render_related(b, code):
    code = code.upper()
    pick_projects(b, [code])
    group = [code] + b.related_projects(code)
    links = []
    for c in b.cards.values():
        if c.project not in group:
            continue
        for d in c.depends_on:
            if card_key(d)[0] != c.project:
                links.append(f"  {c.id} ⟶ waits on {d}")
        for r in c.relates:
            if card_key(r)[0] != c.project:
                links.append(f"  {c.id} ↔ relates {r}")
    text = render_lanes(b, group, title=f"RELATED to {code}")
    if links:
        text += "\n\nCROSS-PROJECT LINKS\n" + "\n".join(links)
    return text


def render_progress(b, codes):
    codes = pick_projects(b, codes)
    out = [f"PROGRESS  cycle {b.cycle}  ·  today {today()}", ""]
    for code in codes:
        s = b.project_stats(code)
        out.append(f"{code:<8} {bar(s['progress'], 20)} {s['progress']:>3}%  "
                   f"{s['counts']['DONE']}/{s['cards']} done  {clip(s['description'], 40)}")
        for c in b.project_cards(code):
            if c.num == 0:
                continue
            w = f" ×{c.weight:g}" if c.weight != 1 else ""
            out.append(f"  {c.id:<8} {bar(c.progress)} {c.progress:>3}%  {c.status:<7} {clip(c.title or '(untitled)', 40)}{w}  {badges(b, c)}".rstrip())
        out.append("")
    out.append("card %: DONE=100, else 'progress' field, else checkboxes [x]/[ ], else WIP=50 / others 0;"
               " project % weighted by numeric fpoint, root card CODE-0 excluded")
    return "\n".join(out)


def render_deps(b, codes):
    codes = pick_projects(b, codes)
    mine = [c for c in b.cards.values() if c.project in codes]
    out = [f"DEPENDENCIES  {', '.join(codes)}  ·  cycle {b.cycle}", ""]

    def chain(cid, depth, seen):
        if depth > 6 or cid in seen:
            return
        seen = seen | {cid}
        c = b.cards.get(cid)
        for d in (c.depends_on if c else []):
            dc = b.cards.get(d)
            mark = "✔" if dc and dc.status == "DONE" else "⛔" if dc else "?"
            desc = f"{dc.status:<7} {clip(dc.title or '(untitled)', 40)}" if dc else "MISSING CARD"
            out.append(f"{'   ' * depth}└ {mark} {d:<8} {desc}")
            if dc and dc.status != "DONE":
                chain(d, depth + 1, seen)

    blocked = [c for c in mine if b.blocked(c)]
    out.append(f"BLOCKED ({len(blocked)})")
    for c in blocked:
        out.append(f"{c.id:<8} {c.status:<7} {clip(c.title or '(untitled)', 40)}")
        chain(c.id, 1, set())
    ready = [c for c in mine if c.status in ("BACKLOG", "TODO") and c.depends_on and not b.open_deps(c)]
    out += ["", f"READY — every dependency done ({len(ready)})"]
    out += [f"{c.id:<8} {c.status:<7} {clip(c.title or '(untitled)', 40)}" for c in ready]
    blocking = sorted(((len([x for x in b.blocks(c.id) if b.cards[x].status != 'DONE']), c) for c in mine
                       if c.status != "DONE"), key=lambda t: -t[0])
    blocking = [(n, c) for n, c in blocking if n]
    out += ["", f"BOTTLENECKS — open cards others wait on ({len(blocking)})"]
    out += [f"{c.id:<8} {c.status:<7} blocks {n}: {', '.join(x for x in b.blocks(c.id) if b.cards[x].status != 'DONE')}"
            for n, c in blocking]
    cyc = [p for p in b.cycles() if any(card_key(x)[0] in codes for x in p)]
    out += ["", f"CYCLES ({len(cyc)})"] + ["  " + " → ".join(p) for p in cyc]
    dang = [(c.id, d) for c in mine for d in c.depends_on + c.relates if d not in b.cards]
    if dang:
        out += ["", "DANGLING LINKS"] + [f"{a:<8} → {d} (no such card in {b.cycle})" for a, d in dang]
    return "\n".join(out)


def render_timeline(b, codes, start=None, days=None, width=60):
    codes = pick_projects(b, codes)
    t0 = today()
    cards = [c for c in b.cards.values() if c.project in codes]
    dates = [d for c in cards for d in (c.start, c.due) if d]
    lo = as_date(parse_date(start)) if start else min(dates + [t0]) - dt.timedelta(days=2)
    hi = lo + dt.timedelta(days=int(days)) if days else max(dates + [t0]) + dt.timedelta(days=3)
    span = max(1, (hi - lo).days)
    step = max(1, -(-span // width))  # days per column
    ncol = -(-span // step)
    col = lambda d: max(0, min(ncol - 1, (d - lo).days // step))
    label_w = 34

    ruler = [" "] * ncol
    marks = [(col(lo), lo.strftime("%b%d"))]
    d = lo + dt.timedelta(days=1)
    while d < hi:
        if d.day == 1:
            marks.append((col(d), d.strftime("%b")))
        elif d.weekday() == 0 and step == 1:
            marks.append((col(d), "|"))
        d += dt.timedelta(days=1)
    free = [True] * ncol
    words = [(i, t) for i, t in marks if len(t) > 1]
    for n, (i, tag) in enumerate(words):  # month names win; the first-day label yields to a close month
        if n + 1 < len(words) and words[n + 1][0] <= i + len(tag):
            continue
        for k, ch in enumerate(tag):
            if i + k < ncol and free[i + k]:
                ruler[i + k], free[i + k] = ch, False
        if i + len(tag) < ncol:
            free[i + len(tag)] = False
    for i, tag in marks:
        if tag == "|" and free[i]:
            ruler[i], free[i] = "|", False
    tline = [" "] * ncol
    if lo <= t0 < hi:
        tline[col(t0)] = "▼"
    out = [f"TIMELINE  {', '.join(codes)}  ·  {lo} → {hi}  ·  1 col = {step} day{'s' if step > 1 else ''}  ·  ▼ today {t0}",
           "", " " * label_w + "".join(tline), " " * label_w + "".join(ruler)]

    def lane(label, a, z, pct, mark=""):
        cells = ["·"] * ncol
        if a or z:
            a, z = a or z, z or a
            i, j = col(max(a, lo)), col(min(z, hi - dt.timedelta(days=1)))
            if z < lo or a >= hi:
                i = j = -1
            n = j - i + 1
            done = round(n * pct / 100)
            for k in range(i, j + 1) if i >= 0 else []:
                cells[k] = "█" if k - i < done else "▒"
        if lo <= t0 < hi and cells[col(t0)] == "·":
            cells[col(t0)] = "│"
        indent = len(label) - len(label.lstrip())
        return pad(" " * indent + clip(label, label_w - 1 - indent), label_w) + "".join(cells) + (f"  {mark}" if mark else "")

    unscheduled = []
    for code in codes:
        s = b.project_stats(code)
        out.append(lane(f"{code} {s['description']}", as_date(s["start"]), as_date(s["due"]), s["progress"],
                        f"{s['progress']}%"))
        for c in b.project_cards(code):
            if c.num == 0:
                continue
            if not (c.start or c.due):
                unscheduled.append(c)
                continue
            mark = ("✔" if c.status == "DONE" else "⏰ overdue" if c.overdue else
                    "⛔ " + ",".join(b.open_deps(c)) if b.blocked(c) else c.status)
            out.append(lane(f"  {c.id} {c.title or '(untitled)'}", c.start, c.due, c.progress, mark))
        out.append("")
    if unscheduled:
        out.append("UNSCHEDULED (set start/due:  ,e  or  :KbSet CARD due=+2w)")
        out += [f"  {c.id:<8} {c.status:<7} {clip(c.title or '(untitled)', 50)}" for c in unscheduled]
    out.append("")
    out.append("█ done share of the span   ▒ remaining   │ today   | week (Mon)   bars run start→due (one date = one-day mark)")
    return "\n".join(out).rstrip()


def render_card(b, c):
    lines = [f"{c.id}  [{c.status}]  {c.kind + ' : ' if c.kind else ''}{c.title or '(untitled)'}",
             f"  path      {os.path.relpath(c.path, b.root)}",
             f"  progress  {bar(c.progress)} {c.progress}%"]
    for k in ("owner", "start", "due", "fpoint", "location", "links"):
        if c.get(k):
            lines.append(f"  {k:<9} {c.get(k)}" + ("  ⏰ OVERDUE" if k == "due" and c.overdue else ""))
    if c.parent:
        lines.append(f"  parent    {c.parent}")
    if c.depends_on:
        lines.append("  depends   " + ", ".join(
            f"{d}{'' if d in b.cards and b.cards[d].status == 'DONE' else ' ⛔'}" for d in c.depends_on))
    bl = b.blocks(c.id)
    if bl:
        lines.append("  blocks    " + ", ".join(bl))
    if c.relates:
        lines.append("  relates   " + ", ".join(c.relates))
    if c.tags:
        lines.append("  tags      " + ", ".join(c.tags))
    return "\n".join(lines)


def validate(b):
    issues = []
    for c in b.cards.values():
        for d in c.depends_on + c.relates:
            if d not in b.cards:
                issues.append(f"{c.id}: link to missing card {d}")
        if c.parent and c.parent not in b.cards and c.num != 0:
            issues.append(f"{c.id}: parent {c.parent} does not exist")
        if c.start and c.due and c.due < c.start:
            issues.append(f"{c.id}: due {c.due} before start {c.start}")
        if c.overdue:
            issues.append(f"{c.id}: overdue since {c.due}")
        if c.status in ("WIP", "DONE") and b.open_deps(c):
            issues.append(f"{c.id}: {c.status} but still waits on {', '.join(b.open_deps(c))}")
        if c.footer_at is None:
            issues.append(f"{c.id}: no JSON footer")
    for p in b.cycles():
        issues.append("dependency cycle: " + " → ".join(p))
    return issues


# ---------------------------------------------------------------- search
SEARCH_MODES = ("cards", "lines", "fields", "count")


def card_lines(c):
    """The searchable text of a card: [(line number, where, text, text to match)]. where = "title", the section
    name, or "meta" (the footer: matched on its values, shown as key=value). Section headers, separators and the
    JSON fence are left out, so a search for "solution" does not hit every card."""
    out, where, titled = [], "body", False
    end = c.footer_at if c.footer_at is not None else len(c.lines)
    for i, line in enumerate(c.lines[:end]):
        text = line.strip()
        if not text:
            continue
        m = SECTION_RE.match(line)
        if m:
            where = m.group(1).strip()
            continue
        if not titled and re.match(r"^#\s+", line):
            titled = True
            text = re.sub(r"^#\s+", "", text)
            out.append((i + 1, "title", text, text))
            continue
        if text.startswith("-----") or text.startswith("```"):
            continue
        out.append((i + 1, where, text, text))
    shown, values = [], []
    for k, v in c.meta.items():
        v = ", ".join(map(str, v)) if isinstance(v, list) else str(v if v is not None else "")
        if v.strip():
            shown.append(f"{k}={v}")
            values.append(v)
    if values:
        out.append(((c.footer_at or end) + 1, "meta", " ".join(shown), " ".join(values)))
    return out


def first_line(c):
    """A card's first line of content: its heading (without "# "), else its first non-empty line."""
    return c.heading or next((l.strip() for l in c.lines if l.strip()), "(empty)")


def search(b, terms=(), regex=False, any_term=False, projects=None, status=None, section=None, owner=None, tag=None):
    """Cards whose text holds every term (any one with any_term), case-insensitive; a term with spaces is a phrase,
    with regex each term is a regular expression. projects/status/owner/tag filter, section restricts the text to
    one section (a name prefix), the title or the meta. Without terms every card passing the filters matches.
    -> [(card, score, hits)] best first; hits = [(line number, where, text)]; score: a title hit counts 5,
    a meta hit 2, a body hit 1 (per term)."""
    pats = []
    for t in terms or ():
        t = str(t).strip()
        if t:
            try:
                pats.append(re.compile(t if regex else re.escape(t), re.I))
            except re.error as e:
                raise KbError(f"bad pattern {t!r}: {e}")
    codes = [x.upper() for x in projects] if projects else None
    want = b._status(status) if status else None
    sec = (section or "").strip().lower()
    out = []
    for c in b.cards.values():
        if (codes and c.project not in codes) or (want and c.status != want):
            continue
        if owner and str(c.get("owner")).lower() != owner.strip().lower().lstrip("@"):
            continue
        if tag and tag.strip().lower() not in [str(x).lower() for x in c.tags]:
            continue
        lines = card_lines(c)
        if sec:
            lines = [l for l in lines if l[1].lower() == sec or (l[1] not in ("title", "meta") and l[1].lower().startswith(sec))]
        if not pats:
            out.append((c, 0, []))
            continue
        found, hits, score = set(), [], 0
        for ln, where, text, hay in lines:
            matched = [k for k, p in enumerate(pats) if p.search(hay)]
            if matched:
                found.update(matched)
                hits.append((ln, where, text))
                score += len(matched) * (5 if where == "title" else 2 if where == "meta" else 1)
        if found and (any_term or len(found) == len(pats)):
            out.append((c, score, hits))
    out.sort(key=lambda r: (-r[1], card_key(r[0].id)))
    return out


def render_search(b, res, terms=(), mode="cards", limit=20, total=None):
    """cards: " - CARD / first line" per card, best first · lines: + up to 3 matching lines each ·
    fields: + status, owner, dates, progress, links · count: matches per project and status."""
    total = len(b.cards) if total is None else total
    what = " ".join(f'"{t}"' if " " in str(t) else str(t) for t in terms) or "(no terms: filters only)"
    head = f"search {what}: {len(res)} of {total} cards ({b.cycle})"
    if not res:
        return head + "\nno card matches"
    if mode == "count":
        def tally(key):
            n = OrderedDict()
            for c, _, _ in res:
                n[key(c)] = n.get(key(c), 0) + 1
            return " · ".join(f"{k} {v}" for k, v in n.items())
        return "\n".join([head, "by project: " + tally(lambda c: c.project),
                          "by status:  " + tally(lambda c: c.status)])
    out = [head]
    limit = max(1, int(limit or 20))
    for c, _, hits in res[:limit]:
        out.append(f" - {c.id} / {first_line(c)}")
        if mode == "lines":
            for ln, where, text in hits[:3]:
                out.append(f"     L{ln} {where}: {clip(text, 100)}")
            if len(hits) > 3:
                out.append(f"     … {len(hits) - 3} more lines")
        elif mode == "fields":
            info = [c.status]
            if c.get("owner"):
                info.append("@" + str(c.get("owner")))
            if c.start or c.due:
                info.append(f"{c.get('start') or '?'} → {c.get('due') or '?'}" + (" OVERDUE" if c.overdue else ""))
            info.append(f"{c.progress}%")
            if c.depends_on:
                info.append("depends " + ",".join(c.depends_on) + (" (open: " + ",".join(b.open_deps(c)) + ")" if b.blocked(c) else ""))
            if c.relates:
                info.append("relates " + ",".join(c.relates))
            if c.tags:
                info.append("tags " + ",".join(map(str, c.tags)))
            out.append("     " + " · ".join(info))
    rest = res[limit:]
    if rest:
        ids = " ".join(c.id for c, _, _ in rest[:30])
        out.append(f" … {len(rest)} more: {ids}" + (" …" if len(rest) > 30 else ""))
    return "\n".join(out)


# ---------------------------------------------------------------- history (git, read-only)
HISTORY_MODES = ("log", "moves", "diff", "show", "pending", "work")
HISTORY_LIMIT = {"log": 20, "moves": 20, "diff": 8, "work": 15}
CARD_PATH_RE = re.compile(r"(?:^|/)(BACKLOG|TODO|HOLD|WIP|DONE)/(archived\.)?([A-Z][A-Z0-9]*-\d+)\.md$")
LOG_FMT = "--format=%x1e%h%x1f%ad%x1f%an%x1f%s"
BOILERPLATE_RE = re.compile(r"^(-{5,}|```(json)?|###\s.*)$")


def run_git(cwd, *args):
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", "-c", "color.ui=never", *args], cwd=cwd,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise KbError(f"git failed to run: {e}")
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise KbError("git: " + (lines[-1] if lines else f"exit {r.returncode}"))
    return r.stdout


def git_repo(path):
    """The repository holding path, or a KbError saying there is no history."""
    try:
        return run_git(path, "rev-parse", "--show-toplevel").strip()
    except KbError:
        raise KbError(f"{path} is not in a git repository: no history")


def card_of(path):
    """(card id, status, archived) of a card path, else None."""
    m = CARD_PATH_RE.search((path or "").replace("\\", "/"))
    return (m.group(3), m.group(1), bool(m.group(2))) if m else None


def history_paths(b, cards=None, projects=None):
    """git pathspecs (relative to the board root) of the current cycle, or of some cards / projects in it, in every
    status folder, archived or not: a status move is a rename between two of them."""
    data = os.path.relpath(b.cycle_dir, b.root).replace(os.sep, "/")
    names = []
    for c in cards or []:
        if not CARD_RE.fullmatch(str(c).upper()):
            raise KbError(f"not a card id: {c}")
        names.append(str(c).upper())
    for p in projects or []:
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", str(p).upper()):
            raise KbError(f"not a project code: {p}")
        names.append(str(p).upper() + "-*")
    if not names:
        return [data]
    return [f":(glob){data}/*/{pre}{n}.md" for n in names for pre in ("", "archived.")]


def _when(v, end=False):
    """A bare date bounds the whole day (git alone reads it as the current time of that day)."""
    v = str(v).strip()
    return v + (" 23:59:59" if end else " 00:00:00") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else v


def log_filters(since=None, until=None, author=None, grep=None, text=None):
    args = [f"--since={_when(since)}"] if since else []
    args += [f"--until={_when(until, end=True)}"] if until else []
    args += [f"--author={author}"] if author else []
    args += [f"--grep={grep}"] if grep else []
    args += [f"-G{text}"] if text else []
    return args + (["-i"] if (author or grep or text) else [])


def git_log(cwd, args, paths=None, limit=20):
    """[{hash, date, author, subject, rest}] newest first; rest = what the log printed under the commit line."""
    out = run_git(cwd, "log", "--date=short", LOG_FMT, f"-n{max(1, int(limit))}", *args, *(["--", *paths] if paths else []))
    commits = []
    for rec in out.split("\x1e")[1:]:
        head, _, rest = rec.partition("\n")
        h, date, author, subject = (head.split("\x1f") + [""] * 4)[:4]
        commits.append({"hash": h, "date": date, "author": author, "subject": subject.strip(), "rest": rest})
    return commits


def name_status(rest):
    """[(A|M|D|R|C|T, old path or None, path)] from --name-status lines."""
    out = []
    for line in rest.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in "AMDRCT":
            out.append((parts[0][:1], parts[1] if len(parts) > 2 else None, parts[-1]))
    return out


def describe_change(st, old, new):
    """What a file change did to the board: "VIM-1 TODO→WIP", "VIM-3 new in BACKLOG", "VIM-2 edited" ..."""
    n, o = card_of(new), card_of(old) if old else None
    if not n:
        return "PROJECT.md edited" if os.path.basename(new or "") == "PROJECT.md" else None
    cid, status, archived = n
    if st == "A":
        return f"{cid} new in {status}"
    if st == "D":
        return f"{cid} deleted"
    if o and o[1] != status:
        return f"{cid} {o[1]}→{status}" + (" (archived)" if archived and not o[2] else "")
    if o and archived and not o[2]:
        return f"{cid} archived"
    return f"{cid} edited"


def patch_files(rest):
    """[{st, old, new, minus, plus}] per file of a -p patch (removed / added lines, without the diff headers)."""
    files, cur = [], None
    for line in rest.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"st": "M", "old": m.group(1) if m else "", "new": m.group(2) if m else "", "minus": [], "plus": [],
                   "hunk": False}
            files.append(cur)
        elif cur is None:
            continue
        elif not cur["hunk"]:
            if line.startswith("new file mode"):
                cur["st"] = "A"
            elif line.startswith("deleted file mode"):
                cur["st"] = "D"
            elif line.startswith("rename from "):
                cur["st"], cur["old"] = "R", line[len("rename from "):]
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to "):]
            elif line.startswith("@@"):
                cur["hunk"] = True
        elif line.startswith("-"):
            cur["minus"].append(line[1:])
        elif line.startswith("+"):
            cur["plus"].append(line[1:])
    return files


def _meta_line(lines):
    for l in reversed(lines):
        t = l.strip()
        if t.startswith("{") and t.endswith("}"):
            try:
                return json.loads(t), l
            except ValueError:
                pass
    return None, None


def _show(v):
    v = ",".join(map(str, v)) if isinstance(v, list) else str(v if v is not None else "")
    return v if v.strip() else "—"


def render_file_change(f, text_re=None, max_lines=6):
    """One changed card of a patch: what happened, the fields that changed (old → new), then the changed lines."""
    desc = describe_change(f["st"], f["old"] if f["st"] == "R" else None, f["new"])
    if not desc:
        return []
    old_meta, old_line = _meta_line(f["minus"])
    new_meta, new_line = _meta_line(f["plus"])
    fields = []
    if old_meta is not None and new_meta is not None:
        for k in list(old_meta) + [k for k in new_meta if k not in old_meta]:
            if old_meta.get(k) != new_meta.get(k):
                fields.append(f"{k}: {_show(old_meta.get(k))} → {_show(new_meta.get(k))}")
    out = ["   " + " · ".join([desc] + fields)]
    changed = [("-", l) for l in f["minus"] if l is not old_line] + [("+", l) for l in f["plus"] if l is not new_line]
    changed = [(sign, l.strip()) for sign, l in changed if l.strip() and not BOILERPLATE_RE.match(l.strip())]
    if text_re:
        changed = [(sign, l) for sign, l in changed if text_re.search(l)]
    for sign, l in changed[:max_lines]:
        out.append(f"     {sign} {clip(l, 100)}")
    if len(changed) > max_lines:
        out.append(f"     … {len(changed) - max_lines} more changed lines")
    return out


def _days(a, z):
    return (as_date(z) - as_date(a)).days if as_date(a) and as_date(z) else "?"


def resolve_rev(b, at):
    """A commit for --at: a revision (hash, HEAD~3), a date (the last commit of that day) or "2 weeks ago"."""
    at = str(at or "HEAD").strip()
    if at.startswith("-"):
        raise KbError(f"bad revision: {at}")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", at):
        rev = run_git(b.root, "rev-list", "-1", f"--before={at} 23:59:59", "HEAD").strip()
    else:
        try:
            rev = run_git(b.root, "rev-parse", "--verify", "--quiet", at + "^{commit}").strip()
        except KbError:
            rev = run_git(b.root, "rev-list", "-1", f"--before={at}", "HEAD").strip()
    if not rev:
        raise KbError(f"no commit at or before {at}")
    h, date, subject = (run_git(b.root, "log", "-1", "--date=short", "--format=%h%x1f%ad%x1f%s", rev).strip().split("\x1f") + [""] * 3)[:3]
    return rev, h, date, subject


def history(b, mode="log", cards=None, projects=None, since=None, until=None, author=None, grep=None, text=None,
            at=None, section=None, repos=None, limit=None):
    """The board's git history, read-only. -> (data for --json, text). Modes:
    log      commits newest first, with what each did to the cards (moves, new, edited)
    moves    each card's status changes with dates, and the days it spent in each status
    diff     per commit and card: the fields that changed (old → new) and the changed lines
    show     one card as it was at a commit or a date (--at), or one of its sections (--in)
    pending  the board's changes not committed yet (staged or not), as in diff
    work     commits in the repositories (--repo, default the board's) whose message names the cards / projects:
             the work done on them, with the files each commit changed"""
    if mode not in HISTORY_MODES:
        raise KbError(f"mode must be one of {', '.join(HISTORY_MODES)}")
    limit = int(limit or HISTORY_LIMIT.get(mode, 20))
    cards = [str(c).upper() for c in cards or []]
    projects = [str(p).upper() for p in projects or []]
    scope = " ".join(cards + [p + "-*" for p in projects]) or f"board {b.cycle}"
    flt = " ".join(f"{k} {v}" for k, v in (("since", since), ("until", until), ("by", author), ("message ~", grep),
                                            ("text ~", text)) if v)
    try:
        text_re = re.compile(text, re.I) if text else None
    except re.error as e:
        raise KbError(f"bad --text pattern: {e}")

    if mode == "work":
        if not (cards or projects):
            raise KbError("work needs --card or --project: it finds the commits whose message names them")
        pats = [rf"(^|[^A-Za-z0-9]){c}([^0-9]|$)" for c in cards] + \
               [rf"(^|[^A-Za-z0-9]){p}([^A-Za-z0-9]|$)" for p in projects]
        for c in cards + projects:
            if not re.fullmatch(r"[A-Z][A-Z0-9]*(-\d+)?", c):
                raise KbError(f"not a card id or project code: {c}")
        data, out = [], []
        home = os.path.expanduser("~")
        for repo in repos or [b.root]:
            top = git_repo(os.path.expanduser(repo))
            commits = git_log(top, ["-E", "--numstat", *[f"--grep={p}" for p in pats],
                                    *log_filters(since, until, author, None, text)], limit=limit)
            label = top.replace(home, "~", 1) if top.startswith(home) else top
            out.append(f"work on {scope} in {label}{' · ' + flt if flt else ''}: {len(commits)} commit"
                       f"{'' if len(commits) == 1 else 's'}{', newest first' if len(commits) > 1 else ''}")
            for c in commits:
                files = []
                for line in c["rest"].splitlines():
                    parts = line.split("\t")
                    if len(parts) == 3:
                        add = int(parts[0]) if parts[0].isdigit() else 0
                        rem = int(parts[1]) if parts[1].isdigit() else 0
                        files.append((parts[2], add, rem))
                files.sort(key=lambda f: -(f[1] + f[2]))
                total_add, total_rem = sum(f[1] for f in files), sum(f[2] for f in files)
                shown = ", ".join(f"{path} +{a} -{r}" for path, a, r in files[:4])
                more = f", … +{len(files) - 4} more" if len(files) > 4 else ""
                out.append(f" {c['hash']} {c['date']} {c['author']}: {clip(c['subject'], 60)} · {len(files)} file"
                           f"{'' if len(files) == 1 else 's'} +{total_add} -{total_rem}: {shown}{more}")
                data.append({"repo": top, "hash": c["hash"], "date": c["date"], "author": c["author"],
                             "subject": c["subject"], "files": [{"path": p, "added": a, "removed": r} for p, a, r in files]})
        return data, "\n".join(out)

    git_repo(b.root)
    paths = history_paths(b, cards, projects)

    if mode == "show":
        if len(cards) != 1:
            raise KbError("show needs exactly one --card")
        rev, h, date, subject = resolve_rev(b, at)
        data_dir = os.path.relpath(b.cycle_dir, b.root).replace(os.sep, "/")
        listing = run_git(b.root, "ls-tree", "-r", "--name-only", rev, "--", data_dir).splitlines()
        path = next((p for p in listing if (card_of(p) or (None,))[0] == cards[0]), None)
        if not path:
            raise KbError(f"{cards[0]} is not on the board at {h} ({date})")
        content = run_git(b.root, "show", f"{rev}:./{path}")
        status = card_of(path)[1]
        if section:
            content = Card(path, status, text=content).section_text(section) or "(empty)"
        head = f"{cards[0]} at {h} {date} ({subject}) in {status}" + (f", section {section}" if section else "") + ":"
        return {"card": cards[0], "hash": h, "date": date, "status": status, "path": path, "text": content}, \
            head + "\n" + content.rstrip("\n")

    if mode == "pending":
        try:
            head_rev = run_git(b.root, "log", "-1", "--date=short", "--format=%h %ad").strip()
        except KbError:
            head_rev = ""
        patch = run_git(b.root, "diff", "HEAD", "--relative", "-M", "-p", "--no-ext-diff", "--", *paths) if head_rev else ""
        untracked = [p for p in run_git(b.root, "ls-files", "--others", "--exclude-standard", "--", *paths).splitlines()
                     if card_of(p)]
        files = patch_files(patch)
        out = [f"uncommitted changes of {scope} (vs {'HEAD ' + head_rev if head_rev else 'no commit yet'}): "
               f"{len(files) + len(untracked)} file{'' if len(files) + len(untracked) == 1 else 's'}"]
        for f in files:
            out += render_file_change(f, text_re)
        for p in untracked:
            out.append(f"   {card_of(p)[0]} new in {card_of(p)[1]} (not added to git)")
        if len(out) == 1:
            out.append("nothing: the board matches the last commit")
        return {"files": [{k: f[k] for k in ("st", "old", "new")} for f in files], "untracked": untracked}, "\n".join(out)

    if mode == "moves":
        commits = git_log(b.root, ["--relative", "-M", "--name-status", *log_filters(since, until, author, grep, text)],
                          paths, limit=5000)
        events = OrderedDict()
        for c in reversed(commits):  # oldest first
            for st, old, new in name_status(c["rest"]):
                n = card_of(new)
                if not n:
                    continue
                o = card_of(old) if old else None
                ev = events.setdefault(n[0], {"steps": [], "last": None})
                ev["last"] = (c["date"], c["hash"])
                if st == "A":
                    ev["steps"].append((c["date"], n[1], "created in"))
                elif st == "D":
                    ev["steps"].append((c["date"], None, "deleted"))
                elif o and o[1] != n[1]:
                    ev["steps"].append((c["date"], n[1], "→"))
        ids = sorted(events, key=card_key)
        out = [f"status moves of {scope}{' · ' + flt if flt else ''}: {len(ids)} card{'' if len(ids) == 1 else 's'}"]
        data = []
        now = today().isoformat()
        for cid in ids[:limit]:
            ev = events[cid]
            card = b.cards.get(cid)
            out.append(f" - {cid} / {first_line(card)}" if card else f" - {cid} (not on the board now)")
            steps = ev["steps"]
            line = " · ".join(f"{d} {kind} {st}".replace(" None", "") if kind != "→" else f"{d} → {st}" for d, st, kind in steps)
            out.append(f"     {line or 'no status change'} · last change {ev['last'][0]} {ev['last'][1]}")
            spent = []
            for i, (d, st, _) in enumerate(steps):
                if st is None:
                    continue
                if i + 1 < len(steps):
                    spent.append(f"{st} {_days(d, steps[i + 1][0])}d")
                elif st != "DONE" and card and card.status == st:
                    spent.append(f"{st} {_days(d, now)}d so far")
            if spent:
                out.append("     days in: " + " · ".join(spent))
            data.append({"card": cid, "steps": [{"date": d, "status": st, "kind": k} for d, st, k in steps],
                         "last": {"date": ev["last"][0], "hash": ev["last"][1]}})
        if len(ids) > limit:
            out.append(f" … {len(ids) - limit} more: {' '.join(ids[limit:limit + 30])}")
        return data, "\n".join(out)

    # log / diff
    patch_mode = mode == "diff"
    commits = git_log(b.root, ["--relative", "-M", *(["-p", "--no-ext-diff"] if patch_mode else ["--name-status"]),
                               *log_filters(since, until, author, grep, text)], paths, limit=limit)
    out = [f"history of {scope}{' · ' + flt if flt else ''}: {len(commits)} commit{'' if len(commits) == 1 else 's'}"
           f"{', newest first' if len(commits) > 1 else ''}" + (f" (showing the last {limit}; -n for more)" if len(commits) == limit else "")]
    data = []
    for c in commits:
        line = f" {c['hash']} {c['date']} {c['author']}: {clip(c['subject'], 50)}"
        if patch_mode:
            out.append(line)
            files = patch_files(c["rest"])
            for f in files:
                out += render_file_change(f, text_re)
            data.append(dict({k: c[k] for k in ("hash", "date", "author", "subject")},
                             files=[{"st": f["st"], "old": f["old"], "new": f["new"], "removed": f["minus"],
                                     "added": f["plus"]} for f in files]))
        else:
            changes = [d for d in (describe_change(*x) for x in name_status(c["rest"])) if d]
            if changes:
                line += " · " + ", ".join(changes[:8]) + (f", +{len(changes) - 8} more" if len(changes) > 8 else "")
            out.append(line)
            data.append(dict({k: c[k] for k in ("hash", "date", "author", "subject")}, changes=changes))
    if not commits:
        out.append("no commit matches")
    return data, "\n".join(out)


# ---------------------------------------------------------------- CLI
def kv_pairs(items):
    out = []
    for it in items:
        if "=" not in it:
            raise KbError(f"expected key=value, got '{it}'")
        k, v = it.split("=", 1)
        out.append((k, v))
    return out


def run(argv, out=print):
    p = argparse.ArgumentParser(prog="kanborg.py", description="kanborg board core")
    p.add_argument("--root", default=os.environ.get("KANBORG_ROOT", "."))
    p.add_argument("--cycle", default=None)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ids")
    sub.add_parser("projects")
    sp = sub.add_parser("list"); sp.add_argument("-p", "--project", nargs="*"); sp.add_argument("-s", "--status")
    sp = sub.add_parser("show"); sp.add_argument("card")
    sp = sub.add_parser("new"); sp.add_argument("code"); sp.add_argument("title", nargs="?", default="")
    sp.add_argument("-s", "--status", default="BACKLOG"); sp.add_argument("-d", "--description", default="")
    sp.add_argument("-k", "--kind", default="")
    sp = sub.add_parser("move"); sp.add_argument("card"); sp.add_argument("status")
    sp = sub.add_parser("set"); sp.add_argument("card"); sp.add_argument("pairs", nargs="+")
    sp = sub.add_parser("link"); sp.add_argument("card"); sp.add_argument("type"); sp.add_argument("other")
    sp = sub.add_parser("unlink"); sp.add_argument("card"); sp.add_argument("args", nargs="+")
    sp = sub.add_parser("journal"); sp.add_argument("card"); sp.add_argument("text"); sp.add_argument("--who", default="")
    sp = sub.add_parser("section"); sp.add_argument("card"); sp.add_argument("name"); sp.add_argument("text", nargs="?")
    sp.add_argument("--append", action="store_true")
    sp = sub.add_parser("project"); sp.add_argument("code"); sp.add_argument("pairs", nargs="*")
    sub.add_parser("overview")
    for name in ("board", "lanes", "progress", "deps"):
        sp = sub.add_parser(name); sp.add_argument("codes", nargs="*")
    sp = sub.add_parser("related"); sp.add_argument("code")
    sp = sub.add_parser("timeline"); sp.add_argument("codes", nargs="*")
    sp.add_argument("--from", dest="start"); sp.add_argument("--days", type=int)
    sub.add_parser("validate")
    sp = sub.add_parser("search"); sp.add_argument("terms", nargs="*")
    sp.add_argument("-p", "--project", nargs="*"); sp.add_argument("-s", "--status")
    sp.add_argument("--in", dest="section"); sp.add_argument("--owner"); sp.add_argument("--tag")
    sp.add_argument("-r", "--regex", action="store_true"); sp.add_argument("--any", action="store_true")
    sp.add_argument("-m", "--mode", choices=SEARCH_MODES, default="cards")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp = sub.add_parser("history"); sp.add_argument("-m", "--mode", choices=HISTORY_MODES, default="log")
    sp.add_argument("-c", "--card", nargs="*"); sp.add_argument("-p", "--project", nargs="*")
    sp.add_argument("--since"); sp.add_argument("--until"); sp.add_argument("--author")
    sp.add_argument("--grep"); sp.add_argument("--text"); sp.add_argument("--at"); sp.add_argument("--in", dest="section")
    sp.add_argument("--repo", nargs="*"); sp.add_argument("-n", "--limit", type=int)
    a = p.parse_args(argv)
    b = Board(a.root, a.cycle)
    emit = (lambda obj, text: out(json.dumps(obj, ensure_ascii=False, indent=1))) if a.json else \
        (lambda obj, text: out(text))

    if a.cmd == "ids":
        emit({"cards": list(b.cards), "projects": list(b.projects)}, "\n".join(list(b.projects) + list(b.cards)))
    elif a.cmd == "projects":
        stats = [b.project_stats(c) for c in b.projects]
        emit(stats, render_overview(b))
    elif a.cmd == "list":
        cards = [c for c in b.cards.values() if (not a.project or c.project in [x.upper() for x in a.project])
                 and (not a.status or c.status == b._status(a.status))]
        emit([c.summary() for c in cards],
             "\n".join(f"{c.id:<8} {c.status:<7} {clip(c.title or '(untitled)', 50)}  {badges(b, c)}".rstrip()
                       for c in cards))
    elif a.cmd == "show":
        c = b.card(a.card)
        emit(dict(c.summary(), blocks=b.blocks(c.id), blocked=b.blocked(c), meta=c.meta, text="\n".join(c.lines)),
             render_card(b, c))
    elif a.cmd == "new":
        c = b.new_card(a.code, a.title, a.status, a.description, a.kind)
        emit(c.summary(), f"created {c.id} in {c.status}: {os.path.relpath(c.path, b.root)}")
    elif a.cmd == "move":
        c, warn = b.move(a.card, a.status)
        emit(dict(c.summary(), warnings=warn), f"{c.id} → {c.status}" + "".join(f"\n  ⚠ {w}" for w in warn))
    elif a.cmd == "set":
        c, changed = b.set_fields(a.card, kv_pairs(a.pairs))
        emit(c.summary(), f"{c.id}: set {', '.join(changed)}\n" + render_card(b, c))
    elif a.cmd == "link":
        c, key, other = b.link(a.card, a.type, a.other)
        emit(c.summary(), f"{c.id} {key} {other}")
    elif a.cmd == "unlink":
        ltype, other = (a.args[0], a.args[1]) if len(a.args) > 1 else (None, a.args[0])
        c, removed = b.unlink(a.card, other, ltype)
        emit(c.summary(), f"{c.id}: removed {', '.join(removed)} → {other}")
    elif a.cmd == "journal":
        c, entry = b.journal(a.card, a.text, a.who)
        emit({"id": c.id, "entry": entry}, f"{c.id}: {entry}")
    elif a.cmd == "section":
        c = b.card(a.card)
        if a.text is None:
            txt = c.section_text(a.name)
            emit({"id": c.id, "section": a.name, "text": txt}, txt)
        else:
            b.set_section(a.card, a.name, a.text, a.append)
            emit({"id": c.id, "section": a.name}, f"{c.id}: section '{a.name}' {'appended' if a.append else 'written'}")
    elif a.cmd == "project":
        if a.pairs:
            pr = b.set_project(a.code, kv_pairs(a.pairs))
            emit(pr, f"{a.code.upper()}: {json.dumps(pr, ensure_ascii=False)}")
        else:
            s = b.project_stats(a.code.upper())
            emit(s, json.dumps(s, ensure_ascii=False, indent=1))
    elif a.cmd == "overview":
        emit([b.project_stats(c) for c in b.projects], render_overview(b))
    elif a.cmd == "board":
        emit({"codes": pick_projects(b, a.codes)}, render_board(b, a.codes))
    elif a.cmd == "lanes":
        emit({"codes": pick_projects(b, a.codes)}, render_lanes(b, a.codes))
    elif a.cmd == "related":
        emit({"code": a.code.upper(), "related": b.related_projects(a.code.upper())}, render_related(b, a.code))
    elif a.cmd == "progress":
        emit([b.project_stats(c) for c in pick_projects(b, a.codes)], render_progress(b, a.codes))
    elif a.cmd == "deps":
        emit({"cycles": b.cycles()}, render_deps(b, a.codes))
    elif a.cmd == "timeline":
        emit({"codes": pick_projects(b, a.codes)}, render_timeline(b, a.codes, a.start, a.days))
    elif a.cmd == "validate":
        issues = validate(b)
        emit(issues, "\n".join(issues) or "board is consistent")
    elif a.cmd == "search":
        res = search(b, a.terms, a.regex, a.any, a.project, a.status, a.section, a.owner, a.tag)
        emit({"total": len(b.cards), "matches": [dict(c.summary(), score=score, first_line=first_line(c),
                                                      hits=[{"line": ln, "where": w, "text": t} for ln, w, t in hits[:5]])
                                                 for c, score, hits in res[:max(1, a.limit)]], "more": len(res) - min(len(res), max(1, a.limit))},
             render_search(b, res, a.terms, a.mode, a.limit))
    elif a.cmd == "history":
        data, text = history(b, a.mode, a.card, a.project, a.since, a.until, a.author, a.grep, a.text, a.at,
                             a.section, a.repo, a.limit)
        emit(data, text)
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):  # board text has ▶ █ ⛔; Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(run(sys.argv[1:]))
    except KbError as e:
        print(f"kanborg: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
