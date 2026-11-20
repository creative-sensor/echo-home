# hbmx memory

Everything hbmx (the large-file reader, `~/echo-home/.vim/mimd/hbmx`) has learnt about the files it read.
A file is known by its content: `sha256` of the bytes. The same content at another path (a copy, a renamed log,
an inflated archive) reuses this memory; a changed file gets a new entry linked to the old one (`lineage`).

```
catalog.yaml                       every path ever read → sha256, size, mtime, format, last read
.function/hbmx                     shell entry to the engine (hbmx_core.py)
<d1>/<d2>/<d3d4>/                  bucket: 1st, 2nd and 3rd-4th hex digits of the sha256
    <full sha256>/                 one file's memory (its hbmx_path)
        meta.yaml                  the map of the file (below)
        knowledge.yaml             what was learnt: key findings (K1…) + every question answered (Q1…) with its evidence
        .function/<name>           helper scripts for the file (executable; header says what/usage/by)
        .data/grid.json            line index + per-cell stats (newlines, error/warn keyword hits, entropy, time)
        .data/toc.json             headings / definitions (text, markdown, code)
        .data/extract/             files written by extract (matching lines, ranges, time windows)
        .data/inflated-*           random-access copy of a compressed file (mapped as its own entry)
```

A bucket holds one `<full sha256>/` folder per file whose sha256 starts with its digits.

## `meta.yaml` — the map

| key          | what it holds |
|--------------|---------------|
| `identity`   | `class` (structured / semi-structured / unstructured), `format` (csv, jsonl, log.clf, log.iso8601, sqlite, gzip, binary …), confidence, libmagic description and mime, evidence, line ending |
| `structure`  | the format's schema: columns with types, JSON key paths, timestamp layout (pattern, field, first/last, in order), levels, CLF fields, archive members, sqlite tables, gguf/safetensors header … |
| `map.regions`| the index tree: semantic parts with line/byte ranges (header, records → children by hour/day/month, document sections, binary terrain, archive members, model layout) |
| `map.zones`  | the uniform grid (≤ 48 zones; each zooms into ≤ 32 sub-zones): line/byte span, first timestamp, error/warn density — or entropy and terrain class for binaries |
| `landmarks`  | reference points: first/last line, first/last error, error hotspots, time gaps, embedded signatures, and the ones hbmx or the user added |
| `functions`  | registry of the helper scripts: name, path, lang, by (hbmx template / llm), description, usage, runs, last run |
| `knowledge`  | summary of knowledge.yaml (counts, latest findings) |
| `howto`      | how to handle this file next time (generated at probe time, extended by hbmx) |
| `extracts`, `locations`, `reads`, `first_read`, `last_read`, `mapping`, `lineage`, `inflated` | bookkeeping |

## `knowledge.yaml` — what was learnt

`findings`: durable facts about the content (title, detail with its numbers, refs, tags, evidence, who/when, how often
confirmed), distilled after each analysed question, noted by hbmx, or added by the user (`===k <text>`).
`qa`: every question asked on this content with its answer, status, who asked (user or @agent), the evidence trail
(each action and its result header) and the findings it produced. An identical question is answered from here at once.

Remove a file's memory: delete its `<full sha256>/` folder (and its line in catalog.yaml).
