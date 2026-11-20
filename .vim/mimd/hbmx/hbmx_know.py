"""
hbmx_know — the knowledge hbmx extracts while the user works on a file, kept with the file's memory so the next load of
the same content recalls it at once.

  <hbmx_path>/knowledge.yaml      (hbmx_path = ~/echo-home/memory/hbmx/<d1>/<d2>/<d3d4>/<full_sha256>/)
    findings   key findings: durable facts about the content (counts, times, top values, anomalies, patterns, where
               things are), each with its evidence, refs and the question it came from. Written by the agent's
               distillation after each analysed question, by the LLM (note kind=finding) and by the user (===k <text>).
               Near-duplicates merge (the finding is "confirmed" again instead of repeated).
    qa         every question answered on this content: question, answer, status, who asked, the evidence trail
               (each action and the header of its result) and the findings it produced.
  meta.yaml keeps a summary (counts, latest titles) under `knowledge`.

Recall: an identical question is answered from qa at once; related findings and past answers are put in front of the
LLM for every request (render_llm); recall(query) searches both, and the previous content of the same path (lineage).
"""
import json
import os
import re
from typing import Dict, List, Optional, Tuple

import hbmx_core as C

KFILE = "knowledge.yaml"
MAX_QA = 1000
STOP = set("""a an the and or but if of to in on at by for with from into over under about between this that these those
it its is are was were be been being do does did done have has had having can could would should will shall may might
must not no yes than then there here what which who whom whose when where why how much many any some all each every
please show tell give find list get me my we our you your i file files log logs line lines data look see check
analyse analyze analysis answer question also just only more most less very""".split())


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.:\-]{1,}", str(text).lower()) if w not in STOP and len(w) > 2}


def norm_q(q: str) -> str:
    return " ".join(re.sub(r"[?!.。]+$", "", str(q).strip().lower()).split())


def path(e) -> str:
    return os.path.join(e.edir, KFILE)


def load(e) -> dict:
    """The knowledge of an entry (cached on it). Findings an older hbmx kept in meta.yaml move here."""
    k = getattr(e, "_know", None)
    if k is not None:
        return k
    try:
        with open(path(e), encoding="utf-8") as f:
            k = C.yload(f.read()) or {}
    except (OSError, ValueError):
        k = {}
    if not isinstance(k, dict):
        k = {}
    k.setdefault("hbmx_knowledge", 1)
    k.setdefault("sha256", e.sha)
    k.setdefault("findings", [])
    k.setdefault("qa", [])
    old = e.meta.pop("findings", None)
    e._know = k
    if old:
        for f in old:
            add_finding(e, f.get("title", ""), f.get("detail", ""), f.get("refs") or [], by=f.get("by") or "hbmx",
                        save_now=False)
        save(e)
    return k


def save(e):
    k = load(e)
    C.write_atomic(path(e), C.ydump(k))
    fs, qs = k["findings"], k["qa"]
    e.meta["knowledge"] = {"file": KFILE, "findings": len(fs), "questions": len(qs),
                           "updated": C.now_iso(), "latest": [f["title"] for f in fs[-5:]]}
    e.save()


def _next(items: List[dict], prefix: str) -> str:
    n = max([int(x["id"][1:]) for x in items if str(x.get("id", "")).startswith(prefix) and x["id"][1:].isdigit()] + [0])
    return f"{prefix}{n + 1}"


def _similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 1.0 if norm_q(a) == norm_q(b) else 0.0
    return len(wa & wb) / len(wa | wb)


def add_finding(e, title: str, detail: str = "", refs=None, tags=None, by: str = "hbmx", qid: Optional[str] = None,
                evidence: Optional[List[str]] = None, save_now: bool = True) -> Tuple[str, bool]:
    """(id, added). A near-duplicate of an existing finding confirms it (refs merged, newer detail kept)."""
    title = C.clip(title, 160)
    if not title:
        raise C.HbmxError("a finding needs a title")
    k = load(e)
    refs = [C.clip(r, 40) for r in (refs or []) if str(r).strip()][:12]
    for f in k["findings"]:
        if norm_q(f["title"]) == norm_q(title) or (_similar(f["title"], title) >= 0.6 and
                                                    _similar(f.get("detail", ""), detail) >= 0.4):
            f["confirmed"] = int(f.get("confirmed") or 1) + 1
            f["last_seen"] = C.now_iso()
            if detail and len(detail) >= len(f.get("detail") or ""):
                f["detail"] = C.clip(detail, 800)
            f["refs"] = list(dict.fromkeys((f.get("refs") or []) + refs))[:12]
            if qid and qid not in (f.get("questions") or []):
                f.setdefault("questions", []).append(qid)
            if save_now:
                save(e)
            return f["id"], False
    rec = {"id": _next(k["findings"], "K"), "title": title, "detail": C.clip(detail, 800), "refs": refs,
           "tags": [C.clip(t, 24) for t in (tags or [])][:6], "by": by, "at": C.now_iso()}
    if qid:
        rec["questions"] = [qid]
    if evidence:
        rec["evidence"] = [C.clip(x, 200) for x in evidence][:4]
    k["findings"].append(rec)
    if save_now:
        save(e)
    return rec["id"], True


def add_qa(e, question: str, answer: str, status: str, by: str, steps: List[dict], findings: List[str]) -> str:
    k = load(e)
    rec = {"id": _next(k["qa"], "Q"), "at": C.now_iso(), "by": by, "question": C.clip(question, 500),
           "answer": str(answer or "")[:2000], "status": status,
           "steps": [{"do": C.clip(s.get("do"), 160), "got": C.clip(s.get("got"), 200)} for s in steps][:14],
           "findings": findings}
    k["qa"].append(rec)
    if len(k["qa"]) > MAX_QA:
        k["qa"] = k["qa"][-MAX_QA:]
    save(e)
    return rec["id"]


def link(e, qid: str, fids: List[str]):
    k = load(e)
    for q in k["qa"]:
        if q["id"] == qid:
            q["findings"] = list(dict.fromkeys((q.get("findings") or []) + fids))
    save(e)


def exact(e, question: str) -> Optional[dict]:
    """A successful earlier answer to the same question on the same content."""
    nq = norm_q(question)
    for q in reversed(load(e)["qa"]):
        if q.get("status") == "SUCCESS" and norm_q(q.get("question", "")) == nq and q.get("answer"):
            return q
    return None


def bump_recall(e, qid: str):
    for q in load(e)["qa"]:
        if q["id"] == qid:
            q["recalled"] = int(q.get("recalled") or 0) + 1
    save(e)


def related(e, text: str, n_find: int = 6, n_qa: int = 3) -> Tuple[List[dict], List[dict]]:
    """Findings and past Q&A ranked by shared words with text (findings fall back to the most recent ones)."""
    k = load(e)
    q = _words(text)

    def score(s):
        w = _words(s)
        return len(q & w)
    fs = sorted(k["findings"], key=lambda f: (score(f["title"] + " " + f.get("detail", "") + " " + " ".join(f.get("tags") or [])),
                                              f.get("last_seen") or f.get("at", "")), reverse=True)[:n_find]
    need = max(2, (len(q) + 1) // 2) if q else 99
    qs = [x for x in k["qa"] if x.get("status") == "SUCCESS" and score(x.get("question", "")) >= need]
    qs = sorted(qs, key=lambda x: (score(x.get("question", "")), x.get("at", "")), reverse=True)[:n_qa]
    return fs, qs


def _fline(f: dict, width: int = 220) -> str:
    conf = f" ✓{f['confirmed']}" if int(f.get("confirmed") or 1) > 1 else ""
    refs = f" (refs {', '.join(f.get('refs') or [])})" if f.get("refs") else ""
    return C.clip(f"{f['id']} [{str(f.get('at', ''))[:10]}{conf}] {f['title']} — {f.get('detail', '')}{refs}", width)


def render_llm(e, text: str, limit: int = 1500) -> str:
    """The [KNOWLEDGE] block of the LLM's live state."""
    k = load(e)
    if not k["findings"] and not k["qa"]:
        return ""
    fs, qs = related(e, text)
    out = [f"[KNOWLEDGE] what earlier analyses of this same content found (sha256 unchanged, so still true): "
           f"{len(k['findings'])} findings, {len(k['qa'])} questions answered"]
    if fs:
        out.append("findings:")
        out += [f"  {_fline(f, 260)}" for f in fs]
    if qs:
        out.append("related past questions:")
        out += [f"  {x['id']} [{x['at'][:10]}] \"{C.clip(x['question'], 100)}\" → {C.clip(x['answer'], 240)}" for x in qs]
    out.append("recall query=… searches all of it")
    t = "\n".join(out)
    return t if len(t) <= limit else t[:limit] + " …"


def render_map(e, n: int = 12) -> List[str]:
    k = load(e)
    out = [f"knowledge ({len(k['findings'])} findings · {len(k['qa'])} questions · {path(e)}):"]
    for f in k["findings"][-n:]:
        out.append(f"  {f['id']:<4} {C.clip(f['title'], 80)}" + (f"  ✓{f['confirmed']}" if int(f.get("confirmed") or 1) > 1 else "")
                   + f"  [{f.get('by')} {str(f.get('at', ''))[:10]}]")
    if k["qa"]:
        out.append("  questions:")
        for q in k["qa"][-8:]:
            mark = "" if q.get("status") == "SUCCESS" else f" ({q.get('status')})"
            out.append(f"  {q['id']:<4} {C.clip(q['question'], 90)}{mark}  [{q.get('by')} {q['at'][:10]}]")
    if len(out) == 1:
        out.append("  (nothing yet: questions answered on this file are kept here)")
    return out


def render_record(e, rid: str) -> str:
    k = load(e)
    for f in k["findings"]:
        if f["id"] == rid:
            out = [f"# finding {rid} · {e.name}", f"  title: {f['title']}", f"  detail: {f.get('detail', '')}",
                   f"  refs: {', '.join(f.get('refs') or []) or '-'}", f"  tags: {', '.join(f.get('tags') or []) or '-'}",
                   f"  by {f.get('by')} · {f.get('at')}" + (f" · confirmed {f['confirmed']}× (last {f.get('last_seen')})"
                                                          if int(f.get("confirmed") or 1) > 1 else "")]
            if f.get("evidence"):
                out.append("  evidence:")
                out += [f"    {x}" for x in f["evidence"]]
            for qid in f.get("questions") or []:
                q = next((x for x in k["qa"] if x["id"] == qid), None)
                if q:
                    out.append(f"  from {qid}: \"{C.clip(q['question'], 140)}\"")
            return "\n".join(out)
    for q in k["qa"]:
        if q["id"] == rid:
            out = [f"# question {rid} · {e.name} · {q.get('status')} · asked by {q.get('by')} {q.get('at')}"
                   + (f" · recalled {q['recalled']}×" if q.get("recalled") else ""),
                   f"  question: {q['question']}", "  answer:"]
            out += [f"    {l}" for l in str(q.get("answer", "")).splitlines()]
            if q.get("steps"):
                out.append("  evidence trail:")
                for s in q["steps"]:
                    out.append(f"    ⚙ {s.get('do')}")
                    if s.get("got"):
                        out.append(f"      → {s['got']}")
            if q.get("findings"):
                out.append(f"  findings: {', '.join(q['findings'])}")
            return "\n".join(out)
    raise C.HbmxError(f"no knowledge record {rid}")


def render_all(e) -> str:
    k = load(e)
    out = [f"# knowledge of {e.name} · {len(k['findings'])} findings · {len(k['qa'])} questions · {path(e)}"]
    if k["findings"]:
        out.append("findings:")
        out += [f"  {_fline(f, 400)}" for f in k["findings"]]
    if k["qa"]:
        out.append("questions:")
        out += [f"  {q['id']} [{q['at'][:16]} {q.get('by')} {q.get('status')}] {C.clip(q['question'], 120)}\n"
                f"       → {C.clip(q.get('answer'), 300)}" for q in k["qa"][-40:]]
    return "\n".join(out)


def recall(e, query: str, n: int = 8) -> str:
    """Search findings and past answers (and the previous content of the same path, marked as such)."""
    out = [f"# recall \"{C.clip(query, 80)}\" · {e.name}"]
    entries = [(e, "")]
    prev = (e.meta.get("lineage") or {}).get("previous_sha256")
    if prev:
        pe = C.load(prev)
        if pe:
            entries.append((pe, " (PREVIOUS content of this path: may be outdated)"))
    found = False
    for ent, tag in entries:
        fs, qs = related(ent, query, n_find=n, n_qa=n)
        w = _words(query)
        fs = [f for f in fs if w & _words(f["title"] + " " + f.get("detail", "") + " " + " ".join(f.get("tags") or []))]
        if fs:
            found = True
            out.append(f"findings{tag}:")
            out += [f"  {_fline(f, 400)}" for f in fs]
        if qs:
            found = True
            out.append(f"past answers{tag}:")
            out += [f"  {q['id']} [{q['at'][:10]}] \"{C.clip(q['question'], 120)}\" → {C.clip(q['answer'], 400)}" for q in qs]
    if not found:
        out.append("  (nothing related in memory: analyse it, the result will be kept)")
    return "\n".join(out)


def summary_line(e) -> str:
    k = load(e)
    if not k["findings"] and not k["qa"]:
        return ""
    last = k["findings"][-1]["title"] if k["findings"] else k["qa"][-1]["question"]
    return f"🧠 knowledge: {len(k['findings'])} findings, {len(k['qa'])} questions answered — latest: {C.clip(last, 90)}"


DISTILL_PROMPT = """You keep the long-term memory of a data analyst about ONE file. From the exchange below, extract the durable knowledge it established about the file's content: facts that would answer future questions about this file without reading it again (counts, times, top values, anomalies, patterns, where things are).
Rules:
- 0 to 3 findings; an empty list when the exchange established nothing new or only repeats [KNOWN].
- Only facts supported by the RESULTS: copy numbers, times, values and line numbers exactly; never guess or generalise.
- title: a short headline (max 12 words). detail: the fact with its numbers, 1-2 sentences. refs: the line numbers, zones (z7), regions (r1.2) or landmarks (L3) it rests on. tags: 1-3 topic words.
Output ONLY JSON: {"findings":[{"title":"..","detail":"..","refs":[".."],"tags":[".."]}]}"""


def distill_input(e, question: str, answer: str, steps: List[dict], limit: int = 3500) -> str:
    k = load(e)
    known = "; ".join(f["title"] for f in k["findings"][-15:]) or "(nothing yet)"
    res = []
    used = 0
    for s in steps:
        block = f"⚙ {s.get('do')}\n{s.get('full') or s.get('got') or ''}"
        block = block[:900]
        if used + len(block) > limit:
            break
        res.append(block)
        used += len(block)
    return (f"[FILE] {e.name} · {e.fmt} · {C.human(e.size)}" + (f" · {C.num(e.lines)} lines" if e.lines else "")
            + f"\n[KNOWN] {known}\n[QUESTION] {question}\n[ANSWER] {C.clip(answer, 800)}\n[RESULTS]\n" + "\n\n".join(res))


def parse_findings(obj) -> List[dict]:
    items = obj.get("findings") if isinstance(obj, dict) else None
    out = []
    for f in items if isinstance(items, list) else []:
        if isinstance(f, dict) and str(f.get("title") or "").strip():
            refs = f.get("refs") if isinstance(f.get("refs"), list) else ([f["refs"]] if f.get("refs") else [])
            tags = f.get("tags") if isinstance(f.get("tags"), list) else []
            out.append({"title": str(f["title"]), "detail": str(f.get("detail") or ""), "refs": [str(r) for r in refs],
                        "tags": [str(t) for t in tags]})
    return out[:3]
