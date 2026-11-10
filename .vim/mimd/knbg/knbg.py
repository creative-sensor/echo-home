#!/usr/bin/env python
"""
knbg: the Kanborg operator agent. Runs the kanborg tool on behalf of the human user: project manager of the board,
Vim hands in the user's editor, coordinator on the Intelligence Network. Headless backend of knbg.lua (same folder).

Boundary: the kanborg tool (~/echo-home/codev/kanborg) knows nothing about this agent. knbg only uses the tool's
public CLI (<root>/.function/kanborg.py, text or --json) as a subprocess and never imports tool code. When the
kanborg TUI module is loaded in the same Neovim, knbg.lua uses its public Lua API (refresh/view/context), guarded.

Protocol: newline-delimited JSON on stdin/stdout. stdout belongs to the protocol; print() goes to stderr.

backend -> nvim
  {"t":"w","b":"chat"|"iface","text":..,"hl":<highlight group>|null}     append lines
  {"t":"state","busy":b,"phase":..,"queue":n,"auto":b,"approval":<text>|null,"net":{..},"model":..}
  {"t":"req","id":N,"op":"context"|"ex"|"lua"|"refresh"|"view"|"open", ...}   run in the user's editor
  {"t":"show"}   {"t":"exit"}
nvim -> backend
  {"t":"input","text":..,"ctx":{card,column,view,buffer,line}|null}
  {"t":"res","id":N,"ok":b,"out":..,"err":..}
  {"t":"net","op":"join"|"leave"|"say"|"purpose", ...}
  {"t":"cancel"} | {"t":"quit"}
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from typing import Optional

_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ARGS = None

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


# ---------------------------------------------------------------- LLM
class LLM:
    def __init__(self, host, port):
        from openai import OpenAI
        self.base = f"http://{host}:{port}/v1"
        self.client = OpenAI(base_url=self.base, api_key="localm", timeout=600)
        self.model = None

    def ensure(self) -> bool:
        if self.model:
            return True
        try:
            models = self.client.models.list()
            self.model = models.data[0].id if models.data else None
        except Exception:
            self.model = None
        return bool(self.model)

    def stream(self, messages, cancel: threading.Event) -> str:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base} (start it, then ask again)")
        resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.0, stream=True)
        out = ""
        try:
            for chunk in resp:
                if cancel.is_set():
                    break
                if chunk.choices and chunk.choices[0].delta.content:
                    out += chunk.choices[0].delta.content
        finally:
            if cancel.is_set():
                try:
                    resp.close()
                except Exception:
                    pass
        return out

    def json_call(self, system: str, user: str) -> Optional[dict]:
        if not self.ensure():
            raise RuntimeError(f"cannot reach the LLM at {self.base}")
        r = self.client.chat.completions.create(model=self.model, temperature=0.0, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        return parse_llm_json(r.choices[0].message.content or "")


# ---------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You are Kanborg (knbg), the operator of the kanborg tool acting on behalf of the human user: project manager of a git-based kanban board, working in the user's Neovim and, when joined, on the Intelligence Network as the coordinator of other expert agents.

THE BOARD
- Cards are markdown files datum/<CYCLE>/<STATUS>/<CODE>-<N>.md; STATUS is the column: BACKLOG, TODO, HOLD, WIP, DONE.
- Each project (CODE) starts with its root card CODE-0 (the project itself: its start/due is the project span); other cards hang under it.
- Card fields: title (replaces the WHOLE heading text after "# ", so include any kind prefix you want, e.g. "FEATURE : timeline view"), owner, start, due (YYYY-MM-DD, MM-DD, today, +3d, +2w), progress (0-100 or "auto"),
  fpoint (numeric weight in project progress), tags; links: depends (blocked until the other card is DONE), relates, parent.
- Card body sections: ### 0 DESCRIPTION, ### 1 SOLUTION, ### 2 NOTES, ### 3 TEST/VERIFICATION, ### 4 JOURNAL.
- Everything you change is git-tracked (staged, not committed) and the user's board refreshes immediately.

RULES
1. Each turn run ONE action, then read its result. Base every claim on action results, never on guesses.
2. "this card"/"here" = [USER POSITION] card. Resolve unclear card references with list_cards/show_card before changing anything.
3. Before moving a card to WIP/DONE, check its dependencies; report what got unblocked or what still blocks.
4. Record decisions, findings and answers from other agents in the card's journal (journal action), concise and attributed.
5. As coordinator: when a request needs expertise you lack (investigating a machine, code, research), ask the right agent on the network
   (ask_agent; see [NETWORK] members and purposes), then record the outcome on the board. Do not ask agents what the board already says.
6. Editor actions (run_command, run_lua) need the user's approval unless auto mode is on; prefer board actions and open_view/open_card.
   Network requests cannot use editor actions.
7. Finish with declare_result: "reply" is the concrete answer (card ids, dates, numbers, what changed). For progress/timeline/dependency
   questions summarise the view, do not paste it whole.
8. Output ONLY one JSON object with only the fields the action needs.

ACTIONS — "action_type" is exactly one of the names below; all other fields sit at the top level of the same object.
Example: {"action_type": "new_card", "project": "LMEN", "title": "Learning Machine", "status": "BACKLOG", "description": "ML study", "reason": "..."}
Reading the board:
  {"action_type":"overview"}
  {"action_type":"view","view":"lanes|board|related|progress|deps|timeline","projects":["CODE"]}
  {"action_type":"validate"}
  {"action_type":"list_cards","project":"CODE","status":"WIP"}            (both optional)
  {"action_type":"show_card","card":"VIM-2","body":true}                    (body optional)
  {"action_type":"read_section","card":"VIM-2","section":"SOLUTION"}
Changing the board:
  {"action_type":"new_card","project":"CODE","title":"..","status":"BACKLOG","kind":"ISSUE","description":".."}
      (description only when CODE is a NEW project: its first card is CODE-0, the project root)
  {"action_type":"move_card","card":"VIM-2","status":"WIP"}
  {"action_type":"set_fields","card":"VIM-2","fields":{"due":"+2w","owner":"bee","progress":40}}
  {"action_type":"link","card":"VIM-2","type":"depends|relates|parent","other":"HIPG-1"}
  {"action_type":"unlink","card":"VIM-2","other":"HIPG-1","type":"depends"}   (type optional)
  {"action_type":"journal","card":"VIM-2","text":".."}
  {"action_type":"write_section","card":"VIM-2","section":"SOLUTION","text":"..","append":false}
  {"action_type":"set_project","project":"VIM","description":"..","related":"HIPG,GGPU"}
Editor:
  {"action_type":"open_view","view":"timeline","projects":["VIM"]}
  {"action_type":"open_card","card":"VIM-2"}
  {"action_type":"run_command","command":"<Ex command>"}
  {"action_type":"run_lua","code":"<Lua>"}
Network:
  {"action_type":"who"}
  {"action_type":"ask_agent","to":"memphia","text":"..","wait":120}
  {"action_type":"wait_agent","from":"memphia","wait":120}
  {"action_type":"post","text":"..","to":["name"]}                         (to optional = broadcast)
Finish:
  {"action_type":"declare_result","status":"SUCCESS|FAILED","reply":".."}
Every action also takes "reason": one short sentence why.
"""


ACTION_NAMES = ["overview", "view", "validate", "list_cards", "show_card", "read_section", "new_card", "move_card",
                "set_fields", "link", "unlink", "journal", "write_section", "set_project", "open_view", "open_card",
                "run_command", "run_lua", "who", "ask_agent", "wait_agent", "post", "declare_result"]


def normalize_action(act: dict) -> dict:
    """Accept near-misses small models produce: a category as action_type with the real action nested
    ({"action_type":"write","new_card":{...}}), "write:new_card", "write.new_card", "action":"new_card"."""
    kind = str(act.get("action_type") or act.get("action") or "").strip()
    if kind in ACTION_NAMES:
        return dict(act, action_type=kind)
    for sep in (":", ".", "/", " "):
        tail = kind.rsplit(sep, 1)[-1].strip()
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


# ---------------------------------------------------------------- kanborg tool client
VIEWS = ["lanes", "board", "related", "progress", "deps", "timeline"]


class ToolError(Exception):
    pass


class Kanborg:
    """The kanborg tool seen from outside: its CLI (<root>/.function/kanborg.py), text or --json.
    Nothing of the tool is imported, so a tool change cannot crash this process and the agent cannot alter the tool."""

    def __init__(self, root: str, python: str):
        self.root = root
        self.cli = os.path.join(root, ".function", "kanborg.py")
        self.python = python

    def available(self) -> bool:
        return os.path.isfile(self.cli)

    def _run(self, args, as_json=False):
        import subprocess
        cmd = [self.python, self.cli, "--root", self.root] + (["--json"] if as_json else []) + [str(x) for x in args]
        try:
            r = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"kanborg CLI failed to run: {e}")
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            if err.startswith("kanborg: "):
                err = err[len("kanborg: "):]
            elif err:
                err = "kanborg CLI crashed: " + err.splitlines()[-1]
            raise ToolError(err or f"kanborg CLI exit {r.returncode}")
        return r.stdout

    def text(self, *args) -> str:
        return self._run(args).rstrip("\n")

    def json(self, *args):
        try:
            return json.loads(self._run(args, as_json=True))
        except ValueError:
            raise ToolError("kanborg CLI returned invalid JSON")


# ---------------------------------------------------------------- agent
class Cancelled(Exception):

    pass


class Agent:
    def __init__(self, args):
        self.args = args
        self.llm = LLM(args.host, args.port)
        self.tool = Kanborg(args.root, args.tool_python)
        self.running = True
        self.tasks: "queue.Queue" = queue.Queue()
        self.pending = 0
        self.current = None  # (text, origin)
        self.phase = "idle"
        self.cancel = threading.Event()
        self.auto = args.auto or autopilot_allows()
        self.approval = None  # {"text":..., "event": Event, "answer": str}
        self.recent: deque = deque(maxlen=6)
        self.reqs = {}
        self.req_id = 0
        self.rlock = threading.Lock()
        from knbg_iface import IFace
        self.iface = IFace(self, args, self.llm.json_call, iface_log)

    # ----- state
    @property
    def busy(self):
        return self.current is not None

    def has_net_work(self):
        return bool(self.current and self.current[1]) or self.pending > 0

    def describe_busy(self):
        return (f"busy with: {clip(self.current[0], 160)}" if self.current else "idle") + f"; {self.pending} queued"

    def project_codes(self):
        try:
            return ", ".join(self.tool.json("ids").get("projects") or []) or "(none)"
        except ToolError:
            return "(board unreadable)"

    def emit_state(self):
        emit({"t": "state", "busy": self.busy, "phase": self.phase, "queue": self.pending, "auto": self.auto,
              "approval": self.approval["text"] if self.approval else None, "net": self.iface.summary(),
              "model": self.llm.model})

    def set_phase(self, p):
        self.phase = p
        self.emit_state()

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
        if text.startswith("===n"):
            return self.net_command(text[4:])
        if text in ("===h", "?", ":help"):
            return chat(HELP_TEXT, "Comment")
        chat("")
        chat("▶ " + text, "Title")
        if self.busy:
            chat("  (queued after the current task)", "Comment")
        self.submit(text, None, ctx)

    def do_cancel(self):
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
            if origin:
                self.iface.answer(origin, status, reply)
            self.current = None
            self.set_phase("idle")
            if self.iface.connected:
                self.iface.set_state(self.iface.idle_state())

    def context_block(self, origin, ctx) -> str:
        parts = [f"[TODAY] {time.strftime('%Y-%m-%d')}", "[BOARD]", self.tool.text("overview")]
        if origin:
            parts.append(f"[REQUESTER] @{origin.get('from')} on the network ({'private' if origin.get('to') else 'in the room'})."
                         + (" Board changes from the network are NOT allowed (read-only)." if self.args.net_readonly else ""))
        else:
            if ctx is None:
                r = self.nvim("context", timeout=5)
                ctx = r.get("out") if r.get("ok") else None
            if isinstance(ctx, dict):
                pos = f"[USER POSITION] buffer {ctx.get('buffer') or '?'}"
                if ctx.get("column"):
                    pos += f", legacy column {ctx['column']}"
                if isinstance(ctx.get("view"), dict):
                    v = ctx["view"]
                    pos += f", view {v.get('kind')} {' '.join(v.get('codes') or [])}"
                pos += f"\n  line: {clip(ctx.get('line'), 200)}"
                if ctx.get("card"):
                    try:
                        pos += "\n  card under cursor:\n" + self.tool.text("show", ctx["card"])
                    except ToolError:
                        pass
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
            raw = self.llm.stream(messages, self.cancel)
            if self.cancel.is_set():
                raise Cancelled()
            act = parse_llm_json(raw)
            messages.append({"role": "assistant", "content": raw})
            if not act or not act.get("action_type"):
                messages.append({"role": "user", "content": "System: reply with ONE JSON object with \"action_type\"."})
                chat("  (unparsable LLM reply, asking again)", "Comment")
                continue
            act = normalize_action(act)
            kind = str(act["action_type"]).strip()
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
            except ToolError as e:
                evidence = f"Error: {e}"
            except Exception as e:
                evidence = f"Error: {type(e).__name__}: {e}"
            evidence = str(evidence)
            first = evidence.split("\n", 1)[0]
            chat(f"    → {clip(first, 240)}" + (f"  (+{evidence.count(chr(10))} lines)" if "\n" in evidence else ""),
                 "ErrorMsg" if evidence.startswith(("Error", "System: denied")) else "Comment")
            messages.append({"role": "user", "content": f"[RESULT of {kind}]\n{evidence[:self.args.max_output]}"})
        return "FAILED", f"Stopped after {self.args.max_turns} turns without a result."

    # ----- actions (the board is only ever touched through the kanborg CLI)
    def do_action(self, kind, a, origin):
        T = self.tool
        writes = {"new_card", "move_card", "set_fields", "link", "unlink", "journal", "write_section", "set_project"}
        if kind in writes and origin and self.args.net_readonly:
            return "System: denied — board changes from network requests are disabled (--net-readonly)."
        codes = [str(c).upper() for c in (a.get("projects") or []) if c] if isinstance(a.get("projects"), list) else \
            [c.upper() for c in re.split(r"[,\s]+", str(a.get("projects") or "")) if c]
        card = str(a.get("card") or "").strip().upper()

        if kind == "overview":
            return T.text("overview")
        if kind == "view":
            v = str(a.get("view") or "lanes")
            if v not in VIEWS:
                return f"Error: view must be one of {', '.join(VIEWS)}"
            return T.text(*self.view_args(v, codes, a))
        if kind == "validate":
            return T.text("validate")
        if kind == "list_cards":
            args = ["list"]
            if a.get("project"):
                args += ["-p", str(a["project"]).upper()]
            if a.get("status"):
                args += ["-s", str(a["status"])]
            return T.text(*args) or "no matching cards"
        if kind == "show_card":
            out = T.text("show", card)
            if a.get("body"):
                out += "\n\n" + T.json("show", card).get("text", "").split("\n```json")[0]
            return out
        if kind == "read_section":
            return T.text("section", card, str(a.get("section") or "")) or "(empty)"

        if kind in writes:
            out = self.write_action(T, kind, a, card)
            self.refresh_editor()
            return out

        if kind in ("open_view", "open_card", "run_command", "run_lua"):
            if origin:
                return "System: denied — network requests cannot drive the user's editor."
            if kind == "open_view":
                v = str(a.get("view") or "lanes")
                if v not in VIEWS + ["overview"]:
                    return f"Error: view must be one of overview, {', '.join(VIEWS)}"
                r = self.nvim("view", kind=v, codes=codes, text=T.text(*self.view_args(v, codes, a)))
                return "view shown in the editor" if r.get("ok") else f"Error: {r.get('err')}"
            if kind == "open_card":
                r = self.nvim("open", card=card, path=T.json("show", card).get("path"))
                return "card opened in a new tab" if r.get("ok") else f"Error: {r.get('err')}"
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
                f"e.g. {{\"action_type\": \"new_card\", \"project\": \"CODE\", \"title\": \"..\"}}")

    @staticmethod
    def view_args(v, codes, a):
        if v == "overview":
            return ["overview"]
        if v == "related":
            return ["related", (codes or [""])[0]]
        extra = []
        if v == "timeline":
            if a.get("from"):
                extra += ["--from", str(a["from"])]
            if a.get("days"):
                extra += ["--days", str(int(a["days"]))]
        return [v, *codes, *extra]

    def write_action(self, T, kind, a, card):
        if kind == "new_card":
            args = ["new", str(a.get("project") or "").upper(), str(a.get("title") or ""),
                    "-s", str(a.get("status") or "BACKLOG")]
            if a.get("description"):
                args += ["-d", str(a["description"])]
            if a.get("kind"):
                args += ["-k", str(a["kind"])]
            return T.text(*args)
        if kind == "move_card":
            return T.text("move", card, str(a.get("status") or ""))
        if kind == "set_fields":
            f = a.get("fields") or {}
            if not isinstance(f, dict) or not f:
                return "Error: set_fields needs \"fields\": {...}"
            pairs = [f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}" for k, v in f.items()]
            return T.text("set", card, *pairs)
        if kind == "link":
            other = str(a.get("other") or "").strip().upper()
            out = T.text("link", card, str(a.get("type") or "depends"), other)
            if "depends" in out and T.json("show", other).get("status") != "DONE":
                out += f" (⛔ {card} is blocked until {other} is DONE)"
            return out
        if kind == "unlink":
            args = ["unlink", card] + ([str(a["type"])] if a.get("type") else []) + [str(a.get("other") or "").upper()]
            return T.text(*args)
        if kind == "journal":
            return T.text("journal", card, str(a.get("text") or ""), "--who", self.args.journal_as)
        if kind == "write_section":
            args = ["section", card, str(a.get("section") or ""), str(a.get("text") or "")]
            return T.text(*args, *(["--append"] if a.get("append") else []))
        if kind == "set_project":
            pairs = [f"{k}={a[k]}" for k in ("description", "related") if a.get(k) is not None]
            if not pairs:
                return "Error: set_project needs description and/or related"
            return T.text("project", str(a.get("project") or "").upper(), *pairs)

    def refresh_editor(self):
        emit({"t": "req", "id": 0, "op": "refresh"})  # fire and forget


HELP_TEXT = """knbg — Kanborg operator
  <text>   a request (status, plan, create/move/link cards, dates, ask other agents ...)
  ===a     toggle auto-approve for editor actions      ===x  cancel the running task
  ===n     network status   ===nj [name] [purpose] join   ===nl leave   ===ns <text> say   ===nw who
  F8 chat ⇄ IFACE pane · <C-c> cancel · q hide (normal mode)"""


def autopilot_allows() -> bool:
    path = os.path.expanduser("~/echo-home/autopilot.yaml")
    try:
        import yaml
        data = yaml.safe_load(open(path)) or {}
        v = (data.get("knbg") or {}).get("allow")
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
    agent.iface.net.close()
    emit({"t": "exit"})


def main():
    global ARGS
    p = argparse.ArgumentParser(description="knbg: the Kanborg operator agent (backend of knbg.lua)")
    p.add_argument("--root", default=os.environ.get("KANBORG_ROOT", "~/echo-home/codev/kanborg"), help="kanborg tool folder")
    p.add_argument("--tool-python", default=os.environ.get("KANBORG_PYTHON", "python3"), help="python that runs the kanborg CLI")
    p.add_argument("--journal-as", default="knbg", help="author of the journal entries the agent writes")
    p.add_argument("--port", type=int, default=8080, help="port of the local LLM server")
    p.add_argument("--host", default="localhost")
    p.add_argument("--auto", action="store_true", help="auto-approve editor actions (also autopilot.yaml knbg.allow)")
    p.add_argument("--max-turns", type=int, default=14)
    p.add_argument("--max-output", type=int, default=8000)
    p.add_argument("--cmd-timeout", type=int, default=30)
    p.add_argument("--ask-timeout", type=int, default=180, help="seconds ask_agent waits for an answer")
    g = p.add_argument_group("Intelligence Network (iFace)")
    g.add_argument("--net-hub", default=os.environ.get("KNBG_NET_HUB", "127.0.0.1:7788"))
    g.add_argument("--net-network", default="")
    g.add_argument("--net-name", default="", help="join at startup under this name")
    g.add_argument("--net-purpose", default="")
    g.add_argument("--net-max-depth", type=int, default=4)
    g.add_argument("--net-rate", type=int, default=6)
    g.add_argument("--net-readonly", action="store_true", help="network requests may read the board but not change it")
    ARGS = p.parse_args()
    ARGS.root = os.path.abspath(os.path.expanduser(ARGS.root))

    agent = Agent(ARGS)
    threading.Thread(target=agent.worker, daemon=True).start()
    if not agent.tool.available():
        chat(f"❌ kanborg tool not found at {agent.tool.cli} (set g:knbg_root or --root)", "ErrorMsg")
    else:
        try:
            ids = agent.tool.json("ids")
            chat(f"knbg · Kanborg operator · board {ARGS.root} · {len(ids.get('projects') or [])} projects, "
                 f"{len(ids.get('cards') or [])} cards", "Title")
        except ToolError as e:
            chat(f"⚠️ kanborg CLI: {e}", "ErrorMsg")
    llm_ok = agent.llm.ensure()
    chat(f"LLM {agent.llm.base}: " + (agent.llm.model if llm_ok else "not reachable yet (start it; I retry on each request)"),
         "Comment" if llm_ok else "WarningMsg")
    chat("Ask me about the board or tell me what to change. ===h help · F8 network pane", "Comment")
    iface_log("IFACE — Intelligence Network traffic and decisions (F8 back to chat)", "Title")
    if ARGS.net_name:
        agent.iface.join()
    agent.emit_state()
    reader(agent)


if __name__ == "__main__":
    main()
