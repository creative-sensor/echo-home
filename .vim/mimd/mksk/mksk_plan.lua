-- mksk_plan.lua: the built-in plan functions of mksk (requires Neovim >= 0.10).
--
-- Reads, edits and writes plan files in the make-sketch format (~/echo-home/Documents/specs/make-sketch.md):
--   mksk--<plan_name>--YYYY-MM-DD.md, sub-documents separated by '---', each with  # OVERVIEW  # TARGET  # DAG.
--   (Headers are read case-insensitively, "# targets" too, and always written in upper case.)
--
-- Everything works on a staged working copy. Edit functions change the copy only; P.diff() / P.show_diff()
-- show what would change; P.commit() writes TODAY's snapshot (created from the newest one when the day changed;
-- older snapshots are never written); P.discard() drops the edits. A zero plan (P.open without a name) has no
-- name and no file until P.name_plan names it. The agent (mksk.py) calls these through mksk.lua, and they are
-- plain Lua, usable by hand:   :lua require("mksk").plan.edit_target{ target = "cutover", status = "WIP", agent = "bee" }
--
--   files     P.setup{dir, actor}  P.list{dir}  P.latest{dir}  P.open{dir, name}  P.name_plan{name}  P.state()  P.validate()
--             P.file()   (the plan file on disk, nil while there is none)
--   weights   P.set_weights{sub, weights = {target = w}}   P.reweigh()        (W = w + w of every prerequisite, once each)
--   table     P.edit_target{sub, target, description, status, agent, w}       (rows stay sorted by W, descending)
--   targets   P.add_target{sub, target, description, w, deps, notes, status, agent}
--             P.edit_target{..., deps, rename}   P.note{sub, target, key, value | text | remove}   P.remove_target{sub, target}
--   sections  P.add_sub{name, goal}   P.set_overview{sub, key, value}
--   review    P.diff()  P.show_diff()  P.commit()  P.discard()
--   handover  P.brief{sub, target, reply_to}    everything an agent needs to take a target over
-- "sub" may be left out when the target name is unique in the plan; "target" may also be written sub/target.

local P = {}
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop
local vdiff = (vim.text and vim.text.diff) or vim.diff

local STD_COLS = { "Wx", "target", "description", "status", "agent" }
local TITLES = { overview = "OVERVIEW", targets = "TARGET", dag = "DAG" } -- section headers as written to the file
local STD = { target = true, description = true, status = true, agent = true }
local STATUS = { TODO = true, WIP = true, DONE = true }
local NAME_PAT = "^[%w_%.%-]+$"
local DATE_PAT = "%d%d%d%d%-%d%d%-%d%d"

-- open, dir, name (nil for a zero plan), base_file (newest snapshot on disk), base (its lines), stamp,
-- doc (working copy), dirty, conflict, actor
local ST = { actor = "mksk" }
local PREFIX = "mksk--"

-- ---------------------------------------------------------------- small helpers
local function fail(msg) error(msg, 0) end
local function trim(s) return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", "")) end
local function oneline(s) return trim((tostring(s or ""):gsub("[\r\n]+", " "))) end
local function today() return os.date("%Y-%m-%d") end

local function fmt(n)
  if n == math.floor(n) then return ("%d"):format(n) end
  return (("%.2f"):format(n):gsub("0+$", ""))
end

--- Git Bash/MSYS hands nvim paths like /c/Users/..; keep one native spelling (C:/Users/..).
local function native(path)
  if fn.has("win32") == 1 and path then
    local drive, rest = path:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(path) == 0 and fn.filereadable(path) == 0 then path = drive:upper() .. ":/" .. rest end
    path = path:gsub("\\", "/")
  end
  return path
end

local function clean_dir(d)
  return (native(fn.fnamemodify(fn.expand(d), ":p")):gsub("[/\\]$", ""))
end

local function same_file(a, b)
  if a == "" or b == "" then return false end
  a, b = native(fn.fnamemodify(a, ":p")), native(fn.fnamemodify(b, ":p"))
  if fn.has("win32") == 1 then a, b = a:lower(), b:lower() end
  return a == b
end

local function stamp(file)
  local s = file and uv.fs_stat(file)
  return s and (s.mtime.sec .. ":" .. s.mtime.nsec .. ":" .. s.size) or nil
end

local function read_lines(file)
  local lines = fn.readfile(file)
  for i, l in ipairs(lines) do lines[i] = (l:gsub("\r$", "")) end
  return lines
end

--- Snapshots mksk--<name>--YYYY-MM-DD.md in dir (of one plan, or of all), newest first within a plan.
local function snapshots(dir, name)
  local out = {}
  local ok, files = pcall(fn.readdir, dir)
  for _, f in ipairs(ok and files or {}) do
    local n, d = f:match("^mksk%-%-(.-)%-%-(" .. DATE_PAT .. ")%.md$")
    if n and n ~= "" and (name == nil or n == name) then out[#out + 1] = { name = n, date = d, file = dir .. "/" .. f } end
  end
  table.sort(out, function(a, b)
    if a.name ~= b.name then return a.name < b.name end
    return a.date > b.date
  end)
  return out
end

--- The file every approved change goes to: today's snapshot of the plan (nil while a zero plan has no name).
local function today_file()
  if not ST.name then return nil end
  return ST.dir .. "/" .. PREFIX .. ST.name .. "--" .. today() .. ".md"
end

local function today_label()
  return ST.name and fn.fnamemodify(today_file(), ":t") or (PREFIX .. "<name>--" .. today() .. ".md")
end

-- ---------------------------------------------------------------- parse
local function is_fence(l) return l:match("^%s*```") ~= nil end

local function strip_blank(t)
  while #t > 0 and not t[#t]:match("%S") do t[#t] = nil end
  while #t > 0 and not t[1]:match("%S") do table.remove(t, 1) end
  return t
end

local function split_cells(line)
  local s = trim(line):gsub("^|", ""):gsub("|$", "")
  local cells, cur, i = {}, "", 1
  while i <= #s do
    local c = s:sub(i, i)
    if c == "\\" and s:sub(i + 1, i + 1) == "|" then
      cur, i = cur .. "|", i + 2
    elseif c == "|" then
      cells[#cells + 1], cur, i = trim(cur), "", i + 1
    else
      cur, i = cur .. c, i + 1
    end
  end
  cells[#cells + 1] = trim(cur)
  return cells
end

local function parse_wx(s)
  s = trim(s)
  local W, w = s:match("^%[%s*([%d%.]+)%s*,%s*([%d%.]+)%s*%]$")
  if W then return tonumber(W), tonumber(w) end
  return tonumber(s), nil
end

local function parse_targets(sec)
  sec.pre, sec.rows, sec.post, sec.cols = {}, {}, {}, vim.deepcopy(STD_COLS)
  local stage, map = 0, {} -- 0 before the table, 1 header seen, 2 rows, 3 after
  for _, l in ipairs(sec.lines) do
    local is_row = l:match("^%s*|") ~= nil
    if stage == 0 and is_row then
      for i, c in ipairs(split_cells(l)) do
        local key, std = c:lower(), nil
        for _, s in ipairs(STD_COLS) do if s:lower() == key then std = s end end
        if key == "name" then std = "target" end
        if not std then
          std = c
          sec.cols[#sec.cols + 1] = c
        end
        map[i] = std
      end
      stage = 1
    elseif stage == 1 and is_row and l:match("^[%s|:%-]+$") then
      stage = 2
    elseif (stage == 1 or stage == 2) and is_row then
      stage = 2
      local row = { extra = {}, target = "", description = "", status = "", agent = "" }
      for i, c in ipairs(split_cells(l)) do
        local k = map[i]
        if k == "Wx" then
          row.W, row.w = parse_wx(c)
          row.W0 = row.W
        elseif STD[k] then
          row[k] = c
        elseif k then
          row.extra[k] = c
        end
      end
      row.status = row.status:upper()
      if row.target ~= "" then sec.rows[#sec.rows + 1] = row end
    elseif stage >= 1 then
      stage = 3
      sec.post[#sec.post + 1] = l
    else
      sec.pre[#sec.pre + 1] = l
    end
  end
  strip_blank(sec.pre)
  strip_blank(sec.post)
end

local function parse_dag(sec)
  sec.pre, sec.lead, sec.targets, sec.post, sec.fence = {}, {}, {}, {}, "```Makefile"
  local stage, cur = 0, nil -- 0 before the fence, 1 inside, 2 after
  for _, l in ipairs(sec.lines) do
    if stage == 0 then
      if is_fence(l) then
        sec.fence, stage = trim(l), 1
      else
        sec.pre[#sec.pre + 1] = l
      end
    elseif stage == 1 then
      if is_fence(l) then
        stage = 2
      else
        local name, rest = l:match("^([%w_%.%-/]+)%s*:%s*(.-)%s*$")
        if name then
          local deps, comment = rest:match("^(.-)%s*(#.*)$")
          cur = { name = name, deps = vim.split(deps or rest, "%s+", { trimempty = true }), body = {} }
          if comment then cur.body[1] = "    " .. comment end
          sec.targets[#sec.targets + 1] = cur
        elseif cur then
          cur.body[#cur.body + 1] = l
        else
          sec.lead[#sec.lead + 1] = l
        end
      end
    else
      sec.post[#sec.post + 1] = l
    end
  end
  for _, t in ipairs(sec.targets) do
    while #t.body > 0 and not t.body[#t.body]:match("%S") do t.body[#t.body] = nil end
  end
  strip_blank(sec.pre)
  strip_blank(sec.post)
end

local function parse_sub(lines)
  local sub = { head = {}, sections = {} }
  local cur, fence = nil, false
  for _, l in ipairs(lines) do
    if is_fence(l) then fence = not fence end
    local title = not fence and l:match("^#%s+(.-)%s*$")
    if title then
      local k = title:lower()
      local kind = (k == "overview" or k == "dag") and k or (k == "target" or k == "targets") and "targets" or "other"
      cur = { title = title, kind = kind, lines = {} }
      sub.sections[#sub.sections + 1] = cur
    elseif cur then
      cur.lines[#cur.lines + 1] = l
    else
      sub.head[#sub.head + 1] = l
    end
  end
  strip_blank(sub.head)
  for _, s in ipairs(sub.sections) do
    strip_blank(s.lines)
    if s.kind == "targets" then parse_targets(s) elseif s.kind == "dag" then parse_dag(s) end
  end
  return sub
end

local function parse(lines)
  local doc, cur, fence = { subs = {} }, {}, false
  local function close()
    if #strip_blank(cur) > 0 then doc.subs[#doc.subs + 1] = parse_sub(cur) end
    cur = {}
  end
  for _, l in ipairs(lines) do
    if is_fence(l) then fence = not fence end
    if not fence and l:match("^%-%-%-+%s*$") then close() else cur[#cur + 1] = l end
  end
  close()
  return doc
end

-- ---------------------------------------------------------------- model access
local function sec(sub, kind, create)
  for _, s in ipairs(sub.sections) do
    if s.kind == kind then return s end
  end
  if not create then return nil end
  local s = { kind = kind, title = TITLES[kind], lines = {} }
  if kind == "targets" then
    s.pre, s.rows, s.post, s.cols = {}, {}, {}, vim.deepcopy(STD_COLS)
  elseif kind == "dag" then
    s.pre, s.lead, s.targets, s.post, s.fence = {}, {}, {}, {}, "```Makefile"
  end
  local order = { overview = 1, targets = 2, dag = 3 }
  local pos = #sub.sections + 1
  for i, x in ipairs(sub.sections) do
    if (order[x.kind] or 9) > order[kind] then
      pos = i
      break
    end
  end
  table.insert(sub.sections, pos, s)
  return s
end

local function ov_get(sub, key)
  local s = sec(sub, "overview")
  for i, l in ipairs(s and s.lines or {}) do
    local k, v = l:match("^%s*([^:]-)%s*:%s*(.-)%s*$")
    if k and k:lower() == key:lower() then return v, i end
  end
end

local function ov_set(sub, key, value)
  local s = sec(sub, "overview", true)
  local _, i = ov_get(sub, key)
  local line = key .. ": " .. oneline(value)
  if i then s.lines[i] = line else s.lines[#s.lines + 1] = line end
end

local function sub_name(doc, i) return ov_get(doc.subs[i], "name") or ("sub" .. i) end

local function sub_names(doc)
  local out = {}
  for i = 1, #doc.subs do out[i] = sub_name(doc, i) end
  return out
end

local function row_of(doc, si, name)
  local s = sec(doc.subs[si], "targets")
  for _, r in ipairs(s and s.rows or {}) do
    if r.target == name then return r end
  end
end

local function dag_of(doc, si, name)
  local s = sec(doc.subs[si], "dag")
  for _, t in ipairs(s and s.targets or {}) do
    if t.name == name then return t end
  end
end

local function target_names(doc, si)
  local seen, out = {}, {}
  local function add(n) if not seen[n] then seen[n], out[#out + 1] = true, n end end
  local s, d = sec(doc.subs[si], "targets"), sec(doc.subs[si], "dag")
  for _, t in ipairs(d and d.targets or {}) do add(t.name) end
  for _, r in ipairs(s and s.rows or {}) do add(r.target) end
  return out
end

--- A prerequisite as written in the DAG -> sub index, target name ("name/target" crosses sub-documents).
local function locate(doc, si, ref)
  local s, t = ref:match("^(.-)/([^/]+)$")
  if not s then return si, ref end
  for i = 1, #doc.subs do
    if sub_name(doc, i) == s then return i, t end
  end
end

local function exists(doc, si, name) return si ~= nil and (row_of(doc, si, name) or dag_of(doc, si, name)) ~= nil end

--- Every target reached from (si, name), itself excluded, each once. Also: unknown references, and whether it loops.
local function closure(doc, si, name)
  local start = si .. "\0" .. name
  local seen, order, missing, cyclic = { [start] = true }, {}, {}, false
  local function visit(i, n)
    local d = dag_of(doc, i, n)
    for _, ref in ipairs(d and d.deps or {}) do
      local j, t = locate(doc, i, ref)
      if exists(doc, j, t) then
        local key = j .. "\0" .. t
        if key == start then cyclic = true end
        if not seen[key] then
          seen[key] = true
          order[#order + 1] = { j, t }
          visit(j, t)
        end
      else
        missing[#missing + 1] = ref
      end
    end
  end
  visit(si, name)
  return order, missing, cyclic
end

--- Fill in the own weight of rows that show only W:  w = W - (w of everything it depends on).
local function derive(doc)
  local busy = {}
  local function own(i, n)
    local r = row_of(doc, i, n)
    if not r then return 0 end
    if r.w then return r.w end
    local key = i .. "\0" .. n
    if busy[key] then return 0 end
    busy[key] = true
    local sum = 0
    for _, m in ipairs((closure(doc, i, n))) do sum = sum + own(m[1], m[2]) end
    r.unweighted = r.W == nil or nil
    r.w = math.max(0, (r.W or 0) - sum)
    return r.w
  end
  for i, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    for _, r in ipairs(s and s.rows or {}) do own(i, r.target) end
  end
end

local function whole(doc, si, name)
  local r = row_of(doc, si, name)
  local sum = r and r.w or 0
  for _, m in ipairs((closure(doc, si, name))) do
    local d = row_of(doc, m[1], m[2])
    sum = sum + (d and d.w or 0)
  end
  return sum
end

--- The weight rule: W = own w + the w of every prerequisite (direct or not), once each. Rows sorted by W, descending.
local function reweigh(doc)
  for i, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    for _, r in ipairs(s and s.rows or {}) do r.W = whole(doc, i, r.target) end
  end
  for _, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    if s then
      for idx, r in ipairs(s.rows) do r._i = idx end
      table.sort(s.rows, function(x, y)
        if x.W ~= y.W then return x.W > y.W end
        return x._i < y._i
      end)
      for _, r in ipairs(s.rows) do r._i = nil end
    end
  end
end

local function load_doc(lines)
  local doc = parse(lines)
  derive(doc)
  return doc
end

-- ---------------------------------------------------------------- render
local function wx(row)
  if not row.W then return "" end
  if row.w and row.w ~= row.W then return ("[%s,%s]"):format(fmt(row.W), fmt(row.w)) end
  return fmt(row.W)
end

local function render_table(s, out)
  local grid = { {} }
  for i, c in ipairs(s.cols) do grid[1][i] = c end
  for _, r in ipairs(s.rows) do
    local line = {}
    for i, c in ipairs(s.cols) do
      local v = c == "Wx" and wx(r) or (STD[c] and r[c]) or r.extra[c] or ""
      line[i] = (oneline(v):gsub("|", "\\|"))
    end
    grid[#grid + 1] = line
  end
  local width = {}
  for _, line in ipairs(grid) do
    for i, c in ipairs(line) do width[i] = math.max(width[i] or 3, fn.strdisplaywidth(c)) end
  end
  local function put(line)
    local cells = {}
    for i, c in ipairs(line) do cells[i] = c .. (" "):rep(width[i] - fn.strdisplaywidth(c)) end
    out[#out + 1] = "| " .. table.concat(cells, " | ") .. " |"
  end
  put(grid[1])
  local rule = {}
  for i = 1, #s.cols do rule[i] = ("-"):rep(width[i] + 2) end
  out[#out + 1] = "|" .. table.concat(rule, "|") .. "|"
  for i = 2, #grid do put(grid[i]) end
end

local function render(doc)
  local out = {}
  local function extend(t) for _, l in ipairs(t) do out[#out + 1] = l end end
  for i, sub in ipairs(doc.subs) do
    if i > 1 then extend({ "", "---", "" }) end
    if #sub.head > 0 then
      extend(sub.head)
      out[#out + 1] = ""
    end
    for j, s in ipairs(sub.sections) do
      if j > 1 then out[#out + 1] = "" end
      out[#out + 1] = "# " .. (TITLES[s.kind] or s.title)
      if s.kind == "targets" then
        extend(s.pre)
        render_table(s, out)
        extend(s.post)
      elseif s.kind == "dag" then
        extend(s.pre)
        out[#out + 1] = s.fence
        extend(s.lead)
        for _, t in ipairs(s.targets) do
          -- a blank line above each target name, for readability (not right below the fence)
          if out[#out] ~= s.fence and out[#out]:match("%S") then out[#out + 1] = "" end
          out[#out + 1] = t.name .. ":" .. (#t.deps > 0 and (" " .. table.concat(t.deps, " ")) or "")
          extend(t.body)
        end
        out[#out + 1] = "```"
        extend(s.post)
      else
        extend(s.lines)
      end
    end
  end
  return out
end

-- ---------------------------------------------------------------- session
local function reload(snap)
  ST.base_file = snap and snap.file or nil
  ST.base = snap and read_lines(snap.file) or {}
  ST.stamp = stamp(ST.base_file)
  ST.doc, ST.dirty, ST.conflict = load_doc(ST.base), false, nil
end

--- Follow the disk: another agent or the user may have changed the file, or a new day's snapshot may exist.
local function refresh()
  if not ST.name then return end
  local newest = snapshots(ST.dir, ST.name)[1]
  local changed = (newest and newest.file or nil) ~= ST.base_file or stamp(ST.base_file) ~= ST.stamp
  if not changed then return end
  if ST.dirty then ST.conflict = true else reload(newest) end
end

local function need()
  if not ST.open then fail("no plan is open (open_plan with a plan name; a new name starts a new plan)") end
  refresh()
  return ST.doc
end

local function check_name(name)
  if not name:match(NAME_PAT) or name:find("--", 1, true) then
    fail("a plan name uses letters, digits, '_', '.' and single '-' only: " .. name)
  end
end

local function touch(doc, si) ov_set(doc.subs[si], "updated", today() .. " by " .. ST.actor) end

--- Run an edit on a copy of the working plan; it only replaces the working plan when it succeeds.
local function mutate(f)
  local copy = vim.deepcopy(need())
  local msg = f(copy)
  reweigh(copy)
  ST.doc, ST.dirty = copy, true
  return msg
end

local function find_sub(doc, name)
  if name and name ~= "" then
    for i = 1, #doc.subs do
      if sub_name(doc, i) == name then return i end
    end
    fail(("no sub-document '%s' (have: %s)"):format(name, table.concat(sub_names(doc), ", ")))
  end
  if #doc.subs == 1 then return 1 end
  if #doc.subs == 0 then fail("the plan has no sub-document yet (add_sub first)") end
  fail("which sub-document? pass \"sub\": " .. table.concat(sub_names(doc), ", "))
end

--- {sub?, target} -> sub index, target name. The target must exist.
local function find(doc, a)
  local name, subname = trim(a.target), a.sub
  if name == "" then fail("\"target\" is required") end
  local s, t = name:match("^(.-)/([^/]+)$")
  if s then subname, name = s, t end
  if subname and subname ~= "" then
    local si = find_sub(doc, subname)
    if not exists(doc, si, name) then
      fail(("no target '%s' in %s (have: %s)"):format(name, subname, table.concat(target_names(doc, si), ", ")))
    end
    return si, name
  end
  local hits, all = {}, {}
  for i = 1, #doc.subs do
    if exists(doc, i, name) then hits[#hits + 1] = i end
    vim.list_extend(all, target_names(doc, i))
  end
  if #hits == 1 then return hits[1], name end
  if #hits == 0 then fail(("no target '%s' (have: %s)"):format(name, table.concat(all, ", "))) end
  fail(("target '%s' exists in several sub-documents: pass \"sub\""):format(name))
end

--- The table row and the DAG entry of a target, creating whichever is missing.
local function ensure(doc, si, name)
  local row, dag = row_of(doc, si, name), dag_of(doc, si, name)
  if not row then
    row = { extra = {}, target = name, description = "", status = "TODO", agent = "", w = 0 }
    table.insert(sec(doc.subs[si], "targets", true).rows, row)
  end
  if not dag then
    dag = { name = name, deps = {}, body = {} }
    table.insert(sec(doc.subs[si], "dag", true).targets, dag)
  end
  return row, dag
end

-- ---------------------------------------------------------------- notes (the free-style lines under a DAG target)
local function note_lines(text)
  local out = {}
  for _, l in ipairs(vim.split(tostring(text or ""), "\n", { plain = true })) do
    local t = trim(l):gsub("^#%s?", "")
    if t ~= "" then out[#out + 1] = "    # " .. t end
  end
  return out
end

--- Lines i..j of the note "key" (the key line plus its deeper-indented continuation lines).
local function note_find(dag, key)
  key = trim(key):lower()
  for i, l in ipairs(dag.body) do
    local pad, k = l:match("^%s*#(%s*)([^:]-)%s*:")
    if k and k:lower() == key then
      local j = i
      while dag.body[j + 1] do
        local p = dag.body[j + 1]:match("^%s*#(%s*)%S")
        if p and #p > #pad then j = j + 1 else break end
      end
      return i, j
    end
  end
end

local function note_set(dag, key, value)
  local new = {}
  if type(value) == "table" then
    new[1] = "    # " .. trim(key) .. ":"
    for _, v in ipairs(value) do new[#new + 1] = "    #    - " .. oneline(v) end
  else
    new[1] = "    # " .. trim(key) .. ": " .. oneline(value)
  end
  local i, j = note_find(dag, key)
  if i then
    for _ = i, j do table.remove(dag.body, i) end
    for k, l in ipairs(new) do table.insert(dag.body, i + k - 1, l) end
  else
    vim.list_extend(dag.body, new)
  end
end

local function set_deps(doc, si, dag, deps)
  if type(deps) == "string" then deps = vim.split(deps, "[,%s]+", { trimempty = true }) end
  local out, seen = {}, {}
  for _, d in ipairs(deps or {}) do
    d = trim(d)
    local j, t = locate(doc, si, d)
    if not exists(doc, j, t) then
      fail(("prerequisite '%s' does not exist (targets here: %s; another sub-document: name/target)"):format(
        d, table.concat(target_names(doc, si), ", ")))
    end
    if j == si and t == dag.name then fail("a target cannot depend on itself") end
    if not seen[d] then seen[d], out[#out + 1] = true, d end
  end
  local old = dag.deps
  dag.deps = out
  local _, _, cyclic = closure(doc, si, dag.name)
  if cyclic then
    dag.deps = old
    fail(("these prerequisites would make '%s' depend on itself"):format(dag.name))
  end
end

local function rename_refs(doc, map)
  for i, sub in ipairs(doc.subs) do
    local d = sec(sub, "dag")
    for _, t in ipairs(d and d.targets or {}) do
      for k, ref in ipairs(t.deps) do
        local new = map(i, ref)
        if new then t.deps[k] = new end
      end
    end
  end
end

local function counts(doc)
  local c = { TODO = 0, WIP = 0, DONE = 0, n = 0 }
  for _, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    for _, r in ipairs(s and s.rows or {}) do
      c.n = c.n + 1
      if c[r.status] then c[r.status] = c[r.status] + 1 end
    end
  end
  return c
end

-- ---------------------------------------------------------------- files
function P.setup(a)
  a = a or {}
  if a.dir then ST.dir = clean_dir(a.dir) end
  if a.actor and a.actor ~= "" then ST.actor = a.actor end
  return "ok"
end

--- Plans found in a folder: { {name, latest, snapshots}, ... }
function P.list(a)
  local dir = clean_dir((a and a.dir) or ST.dir or fn.getcwd())
  local seen, out = {}, {}
  for _, s in ipairs(snapshots(dir)) do
    if not seen[s.name] then
      seen[s.name] = { name = s.name, latest = s.date, snapshots = 0 }
      out[#out + 1] = seen[s.name]
    end
    seen[s.name].snapshots = seen[s.name].snapshots + 1
  end
  return out
end

--- The most up-to-date plan in a folder: its newest snapshot by date (then by modification time). nil when none.
function P.latest(a)
  local dir = clean_dir((a and a.dir) or ST.dir or fn.getcwd())
  local best
  for _, s in ipairs(snapshots(dir)) do
    local st = uv.fs_stat(s.file)
    local mt = st and (st.mtime.sec + st.mtime.nsec / 1e9) or 0
    if not best or s.date > best.date or (s.date == best.date and mt > best.mtime) then
      best = { name = s.name, date = s.date, file = s.file, mtime = mt }
    end
  end
  if best then best.mtime = nil end
  return best
end

function P.summary()
  if not ST.open then return "no plan is open" end
  local c = counts(ST.doc)
  local where
  if not ST.name then
    where = "zero plan: no name and no file yet (once named, the first approved change creates " .. today_label() .. ")"
  elseif not ST.base_file then
    where = "new plan, no file yet (the first approved change creates " .. today_label() .. ")"
  elseif same_file(ST.base_file, today_file()) then
    where = ST.base_file
  else
    where = ST.base_file .. " (the first approved change today creates " .. today_label() .. ")"
  end
  return ("plan %s · %s · %d sub-document(s), %d target(s): %d TODO, %d WIP, %d DONE"):format(
    ST.name or "(unnamed)", where, #ST.doc.subs, c.n, c.TODO, c.WIP, c.DONE)
end

--- The plan file on disk the working plan comes from (its newest snapshot); nil while the plan has no file yet.
function P.file()
  if not ST.open then return nil end
  refresh()
  return ST.base_file
end

--- Open the newest snapshot of a plan (a name, or a path to one of its files). A new name starts a new plan;
--- no name starts a zero plan (unnamed, empty, no file until P.name_plan names it and a change is approved).
function P.open(a)
  a = a or {}
  if ST.dirty then fail("there are staged edits: commit or discard them before opening another plan") end
  local dir, name = a.dir or ST.dir or fn.getcwd(), trim(a.name)
  if name == "" then
    ST.open, ST.dir, ST.name = true, clean_dir(dir), nil
    reload(nil)
    return P.summary()
  end
  local d, n = name:gsub("\\", "/"):match("^(.*)/([^/]+)$")
  if d then dir, name = d, n end
  name = name:gsub("%.md$", ""):gsub("%-%-" .. DATE_PAT .. "$", ""):gsub("^mksk%-%-", "")
  check_name(name)
  ST.open, ST.dir, ST.name = true, clean_dir(dir), name
  reload(snapshots(ST.dir, name)[1])
  return P.summary()
end

--- Name the plan before its first save (a zero plan, or a new plan nothing was saved for yet).
function P.name_plan(a)
  need()
  if ST.base_file then
    fail(("the plan is already saved as %s; open_plan with another name starts a separate plan"):format(ST.name))
  end
  local name = trim(a and a.name):gsub("%.md$", ""):gsub("%-%-" .. DATE_PAT .. "$", ""):gsub("^mksk%-%-", "")
  check_name(name)
  if snapshots(ST.dir, name)[1] then
    fail(("a plan named %s already exists here: open_plan %s opens it"):format(name, name))
  end
  ST.name = name
  return ("the plan is named %s: approved changes go to %s"):format(name, today_label())
end

function P.validate()
  local doc, out = need(), {}
  local names = {}
  for i, sub in ipairs(doc.subs) do
    local sn = sub_name(doc, i)
    local function warn(msg) out[#out + 1] = sn .. ": " .. msg end
    if names[sn] then warn("sub-document name is used twice") end
    names[sn] = true
    for _, kind in ipairs({ "overview", "targets", "dag" }) do
      if not sec(sub, kind) then warn("section # " .. TITLES[kind] .. " is missing") end
    end
    if not ov_get(sub, "name") then warn("overview has no name:") end
    if (ov_get(sub, "resume") or "") == "" then warn("overview has no resume: line") end
    local s, seen = sec(sub, "targets"), {}
    for _, r in ipairs(s and s.rows or {}) do
      local t = r.target
      if seen[t] then warn("target '" .. t .. "' is listed twice") end
      seen[t] = true
      if not STATUS[r.status] then warn(("target '%s' has status '%s' (TODO, WIP or DONE)"):format(t, r.status)) end
      if r.status == "WIP" and r.agent == "" then warn("target '" .. t .. "' is WIP but has no agent") end
      if r.unweighted then warn("target '" .. t .. "' has no weight") end
      if not dag_of(doc, i, t) then warn("target '" .. t .. "' is not in the DAG") end
      local order, missing, cyclic = closure(doc, i, t)
      for _, m in ipairs(missing) do warn(("target '%s' depends on unknown '%s'"):format(t, m)) end
      if cyclic then warn("target '" .. t .. "' depends on itself") end
      if r.W0 and math.abs(r.W0 - whole(doc, i, t)) > 1e-9 and not ST.dirty then
        warn(("target '%s' shows W %s but its prerequisites add up to %s"):format(t, fmt(r.W0), fmt(whole(doc, i, t))))
      end
      if r.status ~= "TODO" then
        for _, m in ipairs(order) do
          local d = row_of(doc, m[1], m[2])
          if d and d.status ~= "DONE" and r.status == "DONE" then
            warn(("target '%s' is DONE but prerequisite '%s' is %s"):format(t, m[2], d.status))
          end
        end
      end
    end
    local d = sec(sub, "dag")
    for _, t in ipairs(d and d.targets or {}) do
      if not row_of(doc, i, t.name) then warn("DAG target '" .. t.name .. "' is not in the targets table") end
    end
  end
  return out
end

--- Where work stands: targets in progress, and TODO targets whose prerequisites are all DONE.
local function resume(doc)
  local wip, ready = {}, {}
  for i, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    for _, r in ipairs(s and s.rows or {}) do
      local label = sub_name(doc, i) .. "/" .. r.target
      if r.status == "WIP" then
        wip[#wip + 1] = label .. (r.agent ~= "" and (" (" .. r.agent .. ")") or "")
      elseif r.status == "TODO" then
        local open = false
        for _, m in ipairs((closure(doc, i, r.target))) do
          local d = row_of(doc, m[1], m[2])
          if d and d.status ~= "DONE" then open = true end
        end
        if not open then ready[#ready + 1] = label .. (r.agent ~= "" and (" (" .. r.agent .. ")") or "") end
      end
    end
  end
  return wip, ready
end

--- Everything the agent needs to know about the working plan, in one call.
function P.state()
  if not ST.open then
    return { open = false, dir = clean_dir(ST.dir or fn.getcwd()), plans = P.list({}) }
  end
  local doc = need()
  local targets = {}
  for i, sub in ipairs(doc.subs) do
    local s = sec(sub, "targets")
    for _, r in ipairs(s and s.rows or {}) do
      targets[#targets + 1] = { sub = sub_name(doc, i), target = r.target, status = r.status, agent = r.agent,
        description = r.description, W = r.W or 0, w = r.w or 0 }
    end
  end
  local wip, ready = resume(doc)
  return {
    open = true, name = ST.name, dir = ST.dir, file = today_file() or "", base_file = ST.base_file or "",
    is_new = ST.base_file == nil, new_snapshot = ST.base_file ~= nil and not same_file(ST.base_file, today_file()),
    plans = P.list({}),
    dirty = ST.dirty, conflict = ST.conflict == true, summary = P.summary(),
    text = table.concat(render(doc), "\n"), subs = sub_names(doc), targets = targets,
    warnings = P.validate(), wip = wip, ready = ready,
  }
end

-- ---------------------------------------------------------------- sections
function P.add_sub(a)
  return mutate(function(doc)
    local name = trim(a.name)
    if not name:match(NAME_PAT) then fail("a sub-document name uses letters, digits, '_', '.' and '-' only") end
    for i = 1, #doc.subs do
      if sub_name(doc, i) == name then fail("sub-document '" .. name .. "' already exists") end
    end
    local sub = { head = {}, sections = {} }
    doc.subs[#doc.subs + 1] = sub
    sec(sub, "overview", true)
    ov_set(sub, "name", name)
    ov_set(sub, "goal", a.goal or "")
    ov_set(sub, "resume", a.resume or "nothing started")
    sec(sub, "targets", true)
    sec(sub, "dag", true)
    touch(doc, #doc.subs)
    return "added sub-document " .. name
  end)
end

function P.set_overview(a)
  return mutate(function(doc)
    local si = find_sub(doc, a.sub)
    local key, value = trim(a.key), oneline(a.value)
    if key == "" or key:find(":", 1, true) then fail("\"key\" is required (name, goal, resume, or any other word)") end
    if key:lower() == "name" then
      if not value:match(NAME_PAT) then fail("a sub-document name uses letters, digits, '_', '.' and '-' only") end
      local old = sub_name(doc, si)
      for i = 1, #doc.subs do
        if i ~= si and sub_name(doc, i) == value then fail("sub-document '" .. value .. "' already exists") end
      end
      rename_refs(doc, function(_, ref)
        local s, t = ref:match("^(.-)/([^/]+)$")
        return s == old and (value .. "/" .. t) or nil
      end)
    end
    ov_set(doc.subs[si], key, value)
    touch(doc, si)
    return ("%s: %s set to \"%s\""):format(sub_name(doc, si), key, value)
  end)
end

-- ---------------------------------------------------------------- targets
local function set_w(row, w)
  local n = tonumber(w)
  if not n or n < 0 then fail("a weight is a number, 0 or more: " .. tostring(w)) end
  row.w, row.unweighted = n, nil
end

function P.add_target(a)
  return mutate(function(doc)
    local si = find_sub(doc, a.sub)
    local name = trim(a.target)
    if not name:match(NAME_PAT) then fail("a target name uses letters, digits, '_', '.' and '-' only: " .. name) end
    if exists(doc, si, name) then fail(("target '%s' already exists in %s (use edit_target)"):format(name, sub_name(doc, si))) end
    local row, dag = ensure(doc, si, name)
    row.description = oneline(a.description)
    row.agent = (oneline(a.agent):gsub("^@", ""))
    row.status = a.status and trim(a.status):upper() or "TODO"
    if not STATUS[row.status] then fail("status must be TODO, WIP or DONE") end
    set_w(row, a.w or 1)
    if a.deps then set_deps(doc, si, dag, a.deps) end
    local notes = type(a.notes) == "table" and a.notes or (a.notes and { a.notes } or {})
    for _, n in ipairs(notes) do vim.list_extend(dag.body, note_lines(n)) end
    touch(doc, si)
    reweigh(doc)
    return ("added %s/%s · Wx %s%s"):format(sub_name(doc, si), name, wx(row),
      #dag.deps > 0 and (" · after " .. table.concat(dag.deps, ", ")) or "")
  end)
end

--- Table edit and target edit in one: description, status, agent, w (own weight), deps (replaces the list), rename.
function P.edit_target(a)
  return mutate(function(doc)
    local si, name = find(doc, a)
    local row, dag = ensure(doc, si, name)
    local did = {}
    if a.description ~= nil then
      row.description = oneline(a.description)
      did[#did + 1] = "description"
    end
    if a.status ~= nil then
      local s = trim(a.status):upper()
      if not STATUS[s] then fail("status must be TODO, WIP or DONE") end
      if s == "DONE" and row.status ~= "DONE" and not note_find(dag, "done") then note_set(dag, "done", today()) end
      row.status = s
      did[#did + 1] = "status " .. s
    end
    if a.agent ~= nil then
      row.agent = (oneline(a.agent):gsub("^@", ""))
      did[#did + 1] = "agent " .. (row.agent ~= "" and row.agent or "(none)")
    end
    if a.w ~= nil then
      set_w(row, a.w)
      did[#did + 1] = "w " .. fmt(row.w)
    end
    if a.deps ~= nil then
      set_deps(doc, si, dag, a.deps)
      did[#did + 1] = "prerequisites " .. (#dag.deps > 0 and table.concat(dag.deps, ", ") or "(none)")
    end
    if a.rename ~= nil then
      local new = trim(a.rename)
      if not new:match(NAME_PAT) then fail("a target name uses letters, digits, '_', '.' and '-' only: " .. new) end
      if new ~= name and exists(doc, si, new) then fail("target '" .. new .. "' already exists") end
      local sn = sub_name(doc, si)
      rename_refs(doc, function(i, ref)
        if i == si and ref == name then return new end
        if ref == sn .. "/" .. name then return sn .. "/" .. new end
      end)
      row.target, dag.name = new, new
      did[#did + 1] = "renamed to " .. new
    end
    if #did == 0 then fail("nothing to change: pass description, status, agent, w, deps or rename") end
    touch(doc, si)
    reweigh(doc)
    return ("%s/%s: %s · Wx %s"):format(sub_name(doc, si), row.target, table.concat(did, ", "), wx(row))
  end)
end

--- Weight update: own weights of several targets at once; every W and the row order follow.
function P.set_weights(a)
  return mutate(function(doc)
    if type(a.weights) ~= "table" or next(a.weights) == nil then fail("\"weights\" is {target: w, ...}") end
    local touched, out = {}, {}
    for target, w in pairs(a.weights) do
      local si, name = find(doc, { sub = a.sub, target = target })
      set_w(ensure(doc, si, name), w)
      touched[si] = true
    end
    for si in pairs(touched) do touch(doc, si) end
    reweigh(doc)
    for target in pairs(a.weights) do
      local si, name = find(doc, { sub = a.sub, target = target })
      out[#out + 1] = name .. " " .. wx(row_of(doc, si, name))
    end
    table.sort(out)
    return "weights: " .. table.concat(out, ", ")
  end)
end

--- Recompute every W from the own weights and re-sort the tables (for a file edited by hand).
function P.reweigh()
  return mutate(function(doc)
    for i = 1, #doc.subs do touch(doc, i) end
    return "weights recomputed"
  end)
end

--- Notes under a DAG target: {key, value} sets or replaces "# key: value" (a list value becomes sub-items);
--- {text} appends free lines; {key, remove = true} deletes a note.
function P.note(a)
  return mutate(function(doc)
    local si, name = find(doc, a)
    local _, dag = ensure(doc, si, name)
    local label = sub_name(doc, si) .. "/" .. name
    local msg
    if a.remove then
      local i, j = note_find(dag, a.key or "")
      if not i then fail(("%s has no note '%s'"):format(label, tostring(a.key))) end
      for _ = i, j do table.remove(dag.body, i) end
      msg = ("%s: note '%s' removed"):format(label, a.key)
    elseif a.key and trim(a.key) ~= "" then
      note_set(dag, a.key, a.value == nil and "" or a.value)
      msg = ("%s: note '%s' set"):format(label, trim(a.key))
    elseif a.text and trim(a.text) ~= "" then
      vim.list_extend(dag.body, note_lines(a.text))
      msg = label .. ": note added"
    else
      fail("pass \"key\" and \"value\", or \"text\"")
    end
    touch(doc, si)
    return msg
  end)
end

function P.remove_target(a)
  return mutate(function(doc)
    local si, name = find(doc, a)
    local row = row_of(doc, si, name)
    if row and row.status == "DONE" then fail("DONE targets are never deleted (the snapshot must stay complete)") end
    local users, sn = {}, sub_name(doc, si)
    for i, sub in ipairs(doc.subs) do
      local d = sec(sub, "dag")
      for _, t in ipairs(d and d.targets or {}) do
        for _, ref in ipairs(t.deps) do
          local j, n = locate(doc, i, ref)
          if j == si and n == name then users[#users + 1] = sub_name(doc, i) .. "/" .. t.name end
        end
      end
    end
    if #users > 0 then
      fail(("'%s' is a prerequisite of %s: change their prerequisites first"):format(name, table.concat(users, ", ")))
    end
    local s, d = sec(doc.subs[si], "targets"), sec(doc.subs[si], "dag")
    for i, r in ipairs(s and s.rows or {}) do
      if r.target == name then table.remove(s.rows, i) break end
    end
    for i, t in ipairs(d and d.targets or {}) do
      if t.name == name then table.remove(d.targets, i) break end
    end
    touch(doc, si)
    return ("removed %s/%s"):format(sn, name)
  end)
end

-- ---------------------------------------------------------------- review
--- { changed, text (unified diff), from, to, added, removed }
function P.diff()
  local doc = need()
  local old, new = table.concat(ST.base, "\n"), table.concat(render(doc), "\n")
  local to = today_file() or ("(unnamed plan) " .. today_label())
  if old == new then return { changed = false, text = "", added = 0, removed = 0, to = to } end
  local text = vdiff(#ST.base > 0 and (old .. "\n") or "", new .. "\n", { result_type = "unified", ctxlen = 2 })
  local added, removed = 0, 0
  for l in text:gmatch("[^\n]+") do
    if l:match("^%+") then added = added + 1 elseif l:match("^%-") then removed = removed + 1 end
  end
  return { changed = true, text = text, added = added, removed = removed, from = ST.base_file or "", to = to,
    new_file = ST.base_file == nil or not same_file(ST.base_file, today_file() or ""), named = ST.name ~= nil }
end

--- Side-by-side diff (saved | proposed) in its own tab, for the user's review. q closes it and goes back to the
--- tab it was opened from.
function P.show_diff()
  local doc = need()
  local back = api.nvim_get_current_tabpage()
  local function close()
    pcall(vim.cmd, "tabclose")
    if api.nvim_tabpage_is_valid(back) then api.nvim_set_current_tabpage(back) end
  end
  local function scratch(lines, label)
    local b = api.nvim_get_current_buf()
    vim.bo[b].buftype, vim.bo[b].bufhidden, vim.bo[b].swapfile = "nofile", "wipe", false
    api.nvim_buf_set_lines(b, 0, -1, false, lines)
    vim.bo[b].modifiable, vim.bo[b].filetype = false, "markdown"
    pcall(api.nvim_buf_set_name, b, label)
    vim.keymap.set("n", "q", close, { buffer = b, nowait = true, silent = true })
    vim.wo.wrap = false
    vim.cmd("diffthis")
  end
  vim.cmd("stopinsert") -- the answer "v" was typed in the chat input
  vim.cmd("tabnew")
  scratch(ST.base, "mksk://saved/" .. (ST.base_file and fn.fnamemodify(ST.base_file, ":t") or "(new plan)"))
  vim.cmd("rightbelow vnew")
  scratch(render(doc), "mksk://proposed/" .. today_label())
  return ST.dirty and "saved | proposed, side by side in a new tab (q closes it)" or "no staged edits: both sides are the same"
end

--- Write the working plan to TODAY's snapshot (never to an older one).
function P.commit()
  local doc = need()
  if not ST.dirty then return { saved = false, text = "nothing to save" } end
  if not ST.name then fail("the plan has no name yet: name it (name_plan) before saving") end
  if ST.conflict then fail("the plan file changed on disk after these edits were staged: discard them and redo") end
  local file, lines = today_file(), render(doc)
  local shown = {}
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) and same_file(api.nvim_buf_get_name(b), file) then
      if vim.bo[b].modified then fail("the plan file is open with unsaved edits: write or discard them first (" .. file .. ")") end
      shown[#shown + 1] = b
    end
  end
  if fn.writefile(lines, file) ~= 0 then fail("cannot write " .. file) end
  local created = ST.base_file == nil or not same_file(ST.base_file, file)
  ST.base_file, ST.base, ST.stamp, ST.dirty, ST.conflict = file, lines, stamp(file), false, nil
  ST.doc = load_doc(lines)
  for _, b in ipairs(shown) do -- buffers showing the file follow it
    pcall(api.nvim_buf_call, b, function()
      local view = fn.winsaveview()
      vim.cmd("silent! edit!")
      fn.winrestview(view)
    end)
  end
  return { saved = true, file = file, created = created, lines = #lines,
    text = (created and "created " or "saved ") .. file }
end

function P.discard()
  if not ST.open then return "no plan is open" end
  local had = ST.dirty
  reload(ST.name and snapshots(ST.dir, ST.name)[1] or nil)
  return had and "staged edits dropped" or "nothing was staged"
end

-- ---------------------------------------------------------------- handover
--- Present a target to the agent taking it over: what it is, what it builds on, what waits for it, its notes.
function P.brief(a)
  local doc = need()
  local si, name = find(doc, a)
  local sub, row, dag = doc.subs[si], row_of(doc, si, name), dag_of(doc, si, name)
  row = row or { target = name, description = "", status = "TODO", agent = "" }
  local L = {}
  local function add(s) L[#L + 1] = s end
  add(("TASK BRIEF · plan %s · sub-document %s · target %s"):format(ST.name or "(unnamed)", sub_name(doc, si), name))
  add("goal of the sub-document: " .. (ov_get(sub, "goal") or "(not stated)"))
  add(("target: %s — %s"):format(name, row.description ~= "" and row.description or "(no description)"))
  add(("status: %s · agent: %s · weight Wx %s"):format(row.status, row.agent ~= "" and row.agent or "(nobody)",
    row.W and wx(row) or "(none)"))
  if dag and #dag.deps > 0 then
    add("builds on:")
    for _, ref in ipairs(dag.deps) do
      local j, t = locate(doc, si, ref)
      local r, d = j and row_of(doc, j, t), j and dag_of(doc, j, t)
      add(("  - %s [%s%s] %s"):format(ref, r and r.status or "?", r and r.agent ~= "" and (", " .. r.agent) or "",
        r and r.description or "(unknown target)"))
      for k, l in ipairs(d and d.body or {}) do
        if k > 8 then
          add("        # …")
          break
        end
        add("      " .. trim(l))
      end
    end
  else
    add("builds on: nothing (it can start now)")
  end
  local users = {}
  for i, s in ipairs(doc.subs) do
    local d = sec(s, "dag")
    for _, t in ipairs(d and d.targets or {}) do
      for _, ref in ipairs(t.deps) do
        local j, n = locate(doc, i, ref)
        if j == si and n == name then users[#users + 1] = (i == si and "" or (sub_name(doc, i) .. "/")) .. t.name end
      end
    end
  end
  add("needed by: " .. (#users > 0 and table.concat(users, ", ") or "nothing else"))
  add("notes:")
  if dag and #dag.body > 0 then
    for _, l in ipairs(dag.body) do add("  " .. trim(l)) end
  else
    add("  (none yet)")
  end
  add("plan file: " .. (ST.base_file or today_file() or "(not saved yet)"))
  if a.reply_to and a.reply_to ~= "" then
    add(("Report to @%s when you start, finish or get blocked: \"%s: WIP\" / \"%s: DONE\" / \"%s: blocked, <why>\", "
      .. "with the evidence (files created, job ids, commits, memo). Say so if you cannot take it."):format(
      a.reply_to, name, name, name))
  end
  return table.concat(L, "\n")
end

P._render, P._parse = render, load_doc -- for tests

return P
