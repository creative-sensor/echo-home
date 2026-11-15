# make-sketch

A markdown planning sketch for agents. It gives the big picture of a plan, serves as the map to
follow, and tracks progress (files created, job ids, memos, anything that reflects a change or a
completion). It is kept simple so an agent taking over can spot where to resume.

## File layout

- **One file per day:** `mksk--<plan_name>--YYYY-MM-DD.md`. The `mksk--` prefix marks a plan file.
  The newest date is the live plan; older files are frozen history. Changes always go to today's
  file (created from the newest one on the first change of the day), never to an older one.
- **Sub-documents:** each file holds one or more, separated by a `---` line with a blank line
  above and below.
- **Sections:** every sub-document has exactly three, in this order: `# OVERVIEW`, `# TARGET`,
  `# DAG`. Headers are written in upper case.

## Sections

### OVERVIEW

A few `key: value` lines. Free-form, but these four are expected:

| key     | meaning                                                              |
|---------|----------------------------------------------------------------------|
| name    | identifier of the sub-document, unique within the file               |
| goal    | what this sub-document delivers                                      |
| resume  | one line telling the next agent where to pick up                     |
| updated | date and agent of the last edit                                      |

### TARGET

A flat checklist as a markdown table with five columns:

| column      | content                              |
|-------------|--------------------------------------|
| Wx          | weight of the target: `W` or `[W,w]` |
| target      | name of the target                   |
| description | what the target is                   |
| status      | `TODO`, `WIP` or `DONE`              |
| agent       | name of the agent assigned           |

`Wx` folds complexity, magnitude, priority and similar factors into numbers; higher means
heavier. It has two parts:

- `w` is the internal weight of the target's own task, ignoring its dependencies. A target whose
  own task is simple has a low `w`, however high its `W`.
- `W` is the weight of the target as a whole. It is a sum: the target's own `w` plus the `w` of
  every target it depends on, directly or through other targets, each counted once. A target
  that sits on top of many other targets has a high `W`; a leaf target has `W` equal to `w`.

Dependencies in other sub-documents (`name/target`) count in the sum too.

Example with `inventory` (w 2), `dump_restore: inventory` (w 4) and
`cutover: inventory dump_restore` (w 3): `cutover` has `W` = 3 + 4 + 2 = 9, with `inventory`
counted once although it is reached twice.

Write `[W,w]` to show both, or just `W` when the sub weight is not worth showing (for example a
leaf target, where the two are the same). Every target has a `w`, since `W` is computed from it,
but showing it in the column is optional; `W` is always shown.

The agent fills `Wx` in when planning, on a scale kept consistent within the file. Rows are
sorted by `W` descending so a human sees the heaviest targets first. The table order says nothing
about execution order; that comes from the DAG.

### DAG

A pseudo Makefile in a fenced `Makefile` block. It is the oversight map of the plan:

- `target: prerequisite ...` reuses Makefile ordering syntax to give the hierarchy and the order
  of execution.
- Comment lines under a target are free-style notes with no fixed field definitions. They hold
  the detail another agent needs to resume: inputs, outputs, storage paths, steps, links between
  targets, deadlines, pending items, job ids, files created, memos.
- A dependency on a target in another sub-document is written `name/target`
  (for example `schema-migration/cutover`).

## Template

````markdown
# OVERVIEW
name: schema-migration
goal: move the orders db from pg13 to pg16 with under 5 min downtime
resume: dump_restore is WIP; dump finished, restore not started (see DAG)
updated: 2026-11-15 by agent-kilo

# TARGET
| Wx     | target       | description                          | status | agent      |
|--------|--------------|--------------------------------------|--------|------------|
| [10,1] | verifier     | run regression suite, get sign-off   | TODO   |            |
| [9,3]  | cutover      | switch app traffic to pg16           | TODO   |            |
| [6,4]  | dump_restore | dump pg13, restore into pg16 staging | WIP    | agent-kilo |
| 2      | inventory    | list tables, sizes, extensions       | DONE   | agent-kilo |

# DAG
```Makefile
inventory:
    # input: none
    # output: docs/inventory.csv
    # magnitude: leaf
    # done: 2026-11-14, commit 3fa91c2
dump_restore: inventory
    # links["inventory"]: uses the table list to order the dump
    # storage: /mnt/backup/orders-2026-11-14.dump
    # job id: dump-8841 (finished), restore not started
    # notes: restore needs the pg_trgm extension installed first
    # magnitude: medium
cutover: inventory dump_restore
    # primary steps:
    #    - freeze writes
    #    - replay the WAL delta
    #    - flip the connection string
    # deadline: strictly before Dec 13
    # priority: high
verifier: cutover
    # test suite: run-v23
    # approver to ask: sam smith
```

---

# OVERVIEW
name: client-rollout
goal: ...
resume: nothing started; blocked until schema-migration/cutover is DONE
updated: 2026-11-15 by agent-kilo

# TARGET
| Wx | target | description | status | agent |
|----|--------|-------------|--------|-------|

# DAG
```Makefile
```
````

## Rules for agents

1. **Open the newest file.** If it is not dated today, copy it to today's date and edit only the
   copy.
2. **Find the resume point.** Read the `resume:` line first, then scan the status column: pick up
   `WIP` rows, then any `TODO` row whose DAG prerequisites are all `DONE`.
3. **Claim before working.** Set the row to `WIP` and put your agent name in it.
4. **Leave evidence when done.** Set the row to `DONE` and add comment lines under that target in
   the DAG: files created, job ids, commits, memos.
5. **Keep names in sync.** Every table row is one DAG target with the same name. Keep the table
   sorted by `W` (the first number of `Wx`) descending; take execution order from the DAG only.
6. **Never delete.** `DONE` targets and their notes stay, so each daily snapshot is a complete
   picture.
7. **Update the overview last.** Rewrite `resume:` and `updated:` before you stop.
