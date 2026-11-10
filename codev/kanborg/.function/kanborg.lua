-- kanborg.lua: card tools and multi-views for the kanborg Neovim TUI (loaded by .vimrc.neo).
--
-- The legacy layout (VIEW + BACKLOG/TODO/HOLD/WIP/DONE columns) stays the default and untouched.
-- Everything here works on "the card under the cursor": a column entry, a line of a view, the card
-- shown in the VIEW window, or an open card file. All reads/writes go through .function/kanborg.py,
-- so the TUI and the CLI always agree on the board.
--
-- Public API for other Neovim code (stable): M.context(win) M.refresh() M.view(kind, codes) M.open(card)
-- M.card_at(win) M.run(args, json). g:kanborg_no_maps = 1 loads the module without the "," keymaps.
--
-- Keys (prefix g:kanborg_prefix, default ","):
--   tools   ,e edit fields (form: title/owner/dates/progress/links; :w saves)   ,n new card
--           ,l link   ,u unlink   ,j journal entry   ,m move (status picker)   ,x toggle [ ]/[x]
--   views   ,o portfolio   ,b board   ,w swimlanes   ,r related projects   ,p progress
--           ,d dependencies   ,t timeline   ,v view picker   ,V back to the legacy layout tab
--   other   ,? this help
-- In a view: <CR> open card  -- peek card  <Tab> next view  rr refresh  q close
-- Commands: :KbView {overview|board|lanes|related|progress|deps|timeline} [CODE..]  :KbEdit [CARD]
--           :KbSet [CARD] k=v..  :KbLink TYPE OTHER  :KbUnlink OTHER  :KbJournal TEXT  :KbNew CODE [title]
--           :KbMove STATUS  :KbValidate

local M = {}
local api, fn = vim.api, vim.fn

--- Git Bash/MSYS hands nvim paths like /c/Users/..; native Windows python needs C:/Users/.. (as in memphia.lua).
local function native(path)
  if fn.has("win32") == 1 and path then
    local drive, rest = path:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(path) == 0 and fn.filereadable(path) == 0 then path = drive:upper() .. ":/" .. rest end
    path = path:gsub("\\", "/")
  end
  return path
end
local SCRIPT_DIR = native(fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h"))
M.root = fn.fnamemodify(SCRIPT_DIR, ":h")
M.core = SCRIPT_DIR .. "/kanborg.py"
local STATUSES = { "BACKLOG", "TODO", "HOLD", "WIP", "DONE" }
local VIEWS = { "overview", "board", "lanes", "related", "progress", "deps", "timeline" }
local CARD_PAT = "%u[%u%d]*%-%d+"
local NS = api.nvim_create_namespace("kanborg")

local function python()
  if vim.g.kanborg_python then return vim.g.kanborg_python end
  if fn.has("win32") == 1 and fn.executable("python") == 1 then return "python" end -- python3 may be the Store stub
  return fn.executable("python3") == 1 and "python3" or "python"
end

local function notify(msg, level) vim.notify("kanborg: " .. msg, level or vim.log.levels.INFO) end

--- Run the core; returns stdout (string) or nil, err.
function M.run(args, json)
  local cmd = { python(), M.core, "--root", M.root }
  if json then cmd[#cmd + 1] = "--json" end
  vim.list_extend(cmd, args)
  local r = vim.system(cmd, { text = true, cwd = M.root }):wait()
  if r.code ~= 0 then
    return nil, vim.trim((r.stderr ~= "" and r.stderr or r.stdout):gsub("^kanborg: ", ""))
  end
  local out = r.stdout or ""
  if json then
    local ok, val = pcall(vim.json.decode, out)
    if not ok then return nil, "bad JSON from core" end
    return val
  end
  return out
end

local function run_or_warn(args, json)
  local out, err = M.run(args, json)
  if not out then notify(err, vim.log.levels.ERROR) end
  return out
end

function M.ids()
  local r = M.run({ "ids" }, true)
  return r and r.cards or {}, r and r.projects or {}
end

-- ---------------------------------------------------------------- card under cursor
local function id_in(s)
  return s and s:match(CARD_PAT) or nil
end

function M.card_at(win)
  win = win or api.nvim_get_current_win()
  local buf = api.nvim_win_get_buf(win)
  local shown = vim.b[buf].kanborg_card
  local row = api.nvim_win_get_cursor(win)[1]
  local line = api.nvim_buf_get_lines(buf, row - 1, row, false)[1] or ""
  if shown and shown ~= "" then
    return id_in(fn.fnamemodify(shown, ":t"))
  end
  local name = api.nvim_buf_get_name(buf)
  if name:match("%.md$") and name:match("/(%u+)/[^/]+%.md$") then
    local st = name:match("/(%u+)/[^/]+%.md$")
    if vim.tbl_contains(STATUSES, st) then return id_in(fn.fnamemodify(name, ":t")) end
  end
  return id_in(line)
end

local function need_card()
  local id = M.card_at()
  if not id then notify("no card under the cursor", vim.log.levels.WARN) end
  return id
end

local function project_of(id) return id and id:match("^(%u[%u%d]*)%-") end

--- Legacy column of a window ("BACKLOG" ...) or nil.
local function column_of(win)
  local ext = fn.fnamemodify(api.nvim_buf_get_name(api.nvim_win_get_buf(win or 0)), ":e")
  return vim.tbl_contains(STATUSES, ext) and ext or nil
end

-- ---------------------------------------------------------------- refresh everything that shows the board
function M.refresh()
  for _, win in ipairs(api.nvim_list_wins()) do
    local buf = api.nvim_win_get_buf(win)
    local col = column_of(win)
    if col then
      local cur = api.nvim_win_get_cursor(win)
      api.nvim_win_call(win, function() pcall(fn.LoadColumn, col) end)
      pcall(api.nvim_win_set_cursor, win, { math.min(cur[1], api.nvim_buf_line_count(buf)), cur[2] })
    elseif vim.b[buf].kanborg_card and vim.b[buf].kanborg_card ~= "" then
      local id = id_in(fn.fnamemodify(vim.b[buf].kanborg_card, ":t"))
      local info = id and M.run({ "show", id }, true)
      if info then
        api.nvim_win_call(win, function() pcall(fn.ViewCard, fn.fnamemodify(info.path, ":.")) end)
      end
    elseif vim.b[buf].kanborg_view then
      M.render(buf)
    end
  end
  vim.cmd("silent! checktime")
end

-- ---------------------------------------------------------------- views
local function view_syntax(buf)
  api.nvim_buf_call(buf, function()
    vim.cmd([[
      syntax clear
      syntax match KbCard /\<\u[A-Z0-9]*-\d\+\>/
      syntax match KbBlocked /⛔\S*/
      syntax match KbOverdue /⏰\S*\|OVERDUE\|overdue/
      syntax match KbDoneBar /█\+/
      syntax match KbTodoBar /▒\+\|░\+/
      syntax match KbToday /[▼│]/
      syntax match KbOwner /@\w\+/
      syntax match KbHead /^\(PORTFOLIO\|BOARD\|LANES\|RELATED\|PROGRESS\|DEPENDENCIES\|TIMELINE\|BLOCKED\|READY\|BOTTLENECKS\|CYCLES\|DANGLING\|CROSS-PROJECT\|UNSCHEDULED\|PROJECT \).*$/
      syntax match KbLane /^== .*$/ contains=KbCard,KbBlocked,KbOverdue,KbDoneBar,KbTodoBar
      syntax keyword KbBacklog BACKLOG
      syntax keyword KbTodo TODO
      syntax keyword KbHold HOLD
      syntax keyword KbWip WIP
      syntax keyword KbDone DONE ✔
    ]])
  end)
end

local function define_highlights()
  local hl = {
    KbCard = { fg = "#268bd2", bold = true }, KbBlocked = { fg = "#dc322f", bold = true },
    KbOverdue = { fg = "#cb4b16", bold = true }, KbDoneBar = { fg = "#859900" }, KbTodoBar = { fg = "#93a1a1" },
    KbToday = { fg = "#d33682", bold = true }, KbOwner = { fg = "#6c71c4" },
    KbHead = { fg = "#073642", bold = true, underline = true }, KbLane = { fg = "#002b36", bg = "#eee8d5", bold = true },
    KbBacklog = { fg = "#dc322f" }, KbTodo = { fg = "#b58900" }, KbHold = { fg = "#d33682" },
    KbWip = { fg = "#2aa198" }, KbDone = { fg = "#859900" },
  }
  for k, v in pairs(hl) do v.default = true; api.nvim_set_hl(0, k, v) end
end
define_highlights()

local function view_args(kind, codes)
  if kind == "overview" then return { "overview" } end
  if kind == "related" then return { "related", codes[1] or "" } end
  return vim.list_extend({ kind }, codes)
end

function M.render(buf)
  local v = vim.b[buf].kanborg_view
  local out, err = M.run(view_args(v.kind, v.codes))
  local lines = vim.split(out or ("error: " .. tostring(err)), "\n", { trimempty = false })
  while #lines > 0 and lines[#lines] == "" do lines[#lines] = nil end
  table.insert(lines, "")
  table.insert(lines, "<CR> open card · -- peek · <Tab> next view · rr refresh · q close · " ..
    (vim.g.kanborg_prefix or ",") .. "? tools")
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].modified = false
end

local function view_tab()
  for _, tab in ipairs(api.nvim_list_tabpages()) do
    for _, w in ipairs(api.nvim_tabpage_list_wins(tab)) do
      if vim.b[api.nvim_win_get_buf(w)].kanborg_view then return tab, w end
    end
  end
end

function M.view(kind, codes)
  kind = kind or "overview"
  if not vim.tbl_contains(VIEWS, kind) then
    return notify("views: " .. table.concat(VIEWS, ", "), vim.log.levels.WARN)
  end
  codes = vim.tbl_map(string.upper, codes or {})
  if kind == "related" and #codes == 0 then
    local p = project_of(M.card_at())
    if not p then return notify(":KbView related CODE (or put the cursor on a card)", vim.log.levels.WARN) end
    codes = { p }
  end
  if not M.legacy_tab then M.legacy_tab = api.nvim_list_tabpages()[1] end
  local tab, win = view_tab()
  local buf
  if tab then
    api.nvim_set_current_tabpage(tab)
    api.nvim_set_current_win(win)
    buf = api.nvim_win_get_buf(win)
  else
    vim.cmd("tabnew")
    buf = api.nvim_get_current_buf()
    vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile = "nofile", "hide", false
    vim.wo.wrap, vim.wo.cursorline, vim.wo.list = false, true, false
    M.view_keymaps(buf)
  end
  vim.b[buf].kanborg_view = { kind = kind, codes = codes }
  pcall(api.nvim_buf_set_name, buf, "kanborg://" .. kind .. (#codes > 0 and (" " .. table.concat(codes, ",")) or ""))
  view_syntax(buf)
  M.render(buf)
end

function M.next_view(delta)
  local v = vim.b.kanborg_view
  if not v then return end
  local i = 1
  for k, name in ipairs(VIEWS) do if name == v.kind then i = k end end
  local kind = VIEWS[(i - 1 + (delta or 1)) % #VIEWS + 1]
  local codes = v.codes
  if kind == "related" and #codes == 0 then
    local _, projects = M.ids()
    codes = { project_of(M.card_at()) or projects[1] }
  end
  M.view(kind, codes)
end

function M.pick_view()
  vim.ui.select(VIEWS, { prompt = "kanborg view" }, function(kind)
    if not kind then return end
    if kind == "overview" then return M.view(kind, {}) end
    local default = project_of(M.card_at()) or ""
    vim.ui.input({ prompt = kind .. " for projects (space separated, empty = all): ", default = default,
      completion = "customlist,v:lua.KanborgCompleteProjects" }, function(s)
      if s == nil then return end
      M.view(kind, vim.split(vim.trim(s), "%s+", { trimempty = true }))
    end)
  end)
end

-- ---------------------------------------------------------------- peek / open
function M.peek(id)
  id = id or need_card()
  if not id then return end
  local out = run_or_warn({ "show", id })
  if not out then return end
  local lines = vim.split(vim.trim(out), "\n")
  local w = 20
  for _, l in ipairs(lines) do w = math.max(w, fn.strdisplaywidth(l) + 2) end
  local buf = api.nvim_create_buf(false, true)
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  view_syntax(buf)
  local win = api.nvim_open_win(buf, false, { relative = "cursor", row = 1, col = 0, width = math.min(w, vim.o.columns - 4),
    height = #lines, style = "minimal", border = "rounded", title = " " .. id .. " ", focusable = false })
  api.nvim_create_autocmd({ "CursorMoved", "BufLeave", "InsertEnter" }, { once = true, callback = function()
    if api.nvim_win_is_valid(win) then api.nvim_win_close(win, true) end
  end })
end

function M.open(id)
  id = id or need_card()
  if not id then return end
  local info = run_or_warn({ "show", id }, true)
  if info then vim.cmd("tabnew " .. fn.fnameescape(info.path) .. " | setlocal filetype=markdown") end
end

function M.view_keymaps(buf)
  local o = function(desc) return { buffer = buf, silent = true, nowait = true, desc = desc } end
  vim.keymap.set("n", "<CR>", function() M.open() end, o("open card"))
  vim.keymap.set("n", "--", function() M.peek() end, o("peek card"))
  vim.keymap.set("n", "<Tab>", function() M.next_view(1) end, o("next view"))
  vim.keymap.set("n", "<S-Tab>", function() M.next_view(-1) end, o("previous view"))
  vim.keymap.set("n", "rr", function() M.render(buf) end, o("refresh view"))
  vim.keymap.set("n", "q", function()
    if #api.nvim_list_tabpages() > 1 then vim.cmd("tabclose") end
  end, o("close views"))
  for _, k in ipairs({ "---", "mmm", "bbb" }) do -- legacy column keys make no sense in a view
    vim.keymap.set("n", k, function() notify("in a view use ,m (move) / <CR> (open)") end, o("disabled in views"))
  end
end

-- ---------------------------------------------------------------- field editor (,e)
local FORM_FIELDS = { "title", "owner", "start", "due", "progress", "fpoint", "tags", "parent", "depends", "relates" }

local function form_lines(info)
  local m = info.meta or {}
  local function list(v) return type(v) == "table" and table.concat(v, ", ") or tostring(v or "") end
  local vals = {
    title = info.heading or info.title, owner = m.owner, start = m.start, due = m.due,
    progress = (m.progress ~= nil and m.progress ~= "") and tostring(m.progress) or "auto",
    fpoint = m.fpoint, tags = list(m.tags), parent = m.parent or "",
    depends = list(info.depends_on), relates = list(info.relates),
  }
  local lines = {
    "# " .. info.id .. " [" .. info.status .. "]   :w save · q cancel · empty value clears",
    "# title: the whole heading after '# ' (e.g. FEATURE : timeline view)",
    "# dates: YYYY-MM-DD | MM-DD | today | +3d | +2w    progress: 0-100 or auto (now " .. info.progress .. "%)",
    "# parent: default " .. info.project .. "-0 (project root)    depends/relates: card ids, comma separated",
  }
  for _, k in ipairs(FORM_FIELDS) do
    lines[#lines + 1] = string.format("%-9s= %s", k, vals[k] == nil and "" or tostring(vals[k]))
  end
  return lines, vals
end

local function parse_form(buf)
  local out = {}
  for _, l in ipairs(api.nvim_buf_get_lines(buf, 0, -1, false)) do
    local k, v = l:match("^(%w+)%s*=%s?(.*)$")
    if k then out[k] = vim.trim(v) end
  end
  return out
end

local function split_ids(s)
  local t = {}
  for id in (s or ""):gmatch(CARD_PAT) do t[#t + 1] = id end
  return t
end

function M.edit(id)
  id = id or need_card()
  if not id then return end
  local info = run_or_warn({ "show", id }, true)
  if not info then return end
  local lines, orig = form_lines(info)
  local buf = api.nvim_create_buf(false, true)
  vim.bo[buf].buftype = "acwrite"
  api.nvim_buf_set_name(buf, "kanborg://edit/" .. id)
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modified = false
  local width = math.min(90, vim.o.columns - 6)
  local win = api.nvim_open_win(buf, true, { relative = "editor", width = width, height = #lines,
    row = math.floor((vim.o.lines - #lines) / 2) - 1, col = math.floor((vim.o.columns - width) / 2),
    border = "rounded", title = " edit " .. id .. " ", style = "minimal" })
  api.nvim_win_set_cursor(win, { 5, 11 })
  api.nvim_buf_call(buf, function()
    vim.cmd([[syntax match Comment /^#.*/ | syntax match KbCard /\<\u[A-Z0-9]*-\d\+\>/ | syntax match Identifier /^\w\+/]])
  end)
  local function close()
    if api.nvim_win_is_valid(win) then api.nvim_win_close(win, true) end
    if api.nvim_buf_is_valid(buf) then api.nvim_buf_delete(buf, { force = true }) end
  end
  vim.keymap.set("n", "q", close, { buffer = buf, nowait = true })
  api.nvim_create_autocmd("BufWriteCmd", { buffer = buf, callback = function()
    local new = parse_form(buf)
    local pairs_ = {}
    for _, k in ipairs({ "title", "owner", "start", "due", "progress", "fpoint", "tags", "parent" }) do
      local o = orig[k] == nil and "" or tostring(orig[k])
      if new[k] ~= nil and new[k] ~= o then pairs_[#pairs_ + 1] = k .. "=" .. new[k] end
    end
    local errs = {}
    if #pairs_ > 0 then
      local _, err = M.run(vim.list_extend({ "set", id }, pairs_))
      if err then errs[#errs + 1] = err end
    end
    for _, kind in ipairs({ "depends", "relates" }) do
      local before, after = split_ids(orig[kind]), split_ids(new[kind])
      for _, x in ipairs(after) do
        if not vim.tbl_contains(before, x) then
          local _, err = M.run({ "link", id, kind, x })
          if err then errs[#errs + 1] = err end
        end
      end
      for _, x in ipairs(before) do
        if not vim.tbl_contains(after, x) then
          local _, err = M.run({ "unlink", id, kind, x })
          if err then errs[#errs + 1] = err end
        end
      end
    end
    M.refresh()
    if #errs > 0 then
      vim.schedule(function() notify(table.concat(errs, "\n"), vim.log.levels.ERROR) end)
      local fresh = M.run({ "show", id }, true) -- keep the form open, re-based on what was saved
      if fresh then
        local l2, o2 = form_lines(fresh)
        orig = o2
        local keep = parse_form(buf)
        api.nvim_buf_set_lines(buf, 0, -1, false, l2)
        for i, l in ipairs(l2) do
          local k = l:match("^(%w+)%s*=")
          if k and keep[k] then api.nvim_buf_set_lines(buf, i - 1, i, false, { string.format("%-9s= %s", k, keep[k]) }) end
        end
      end
      vim.bo[buf].modified = true
    else
      vim.bo[buf].modified = false
      notify(id .. " saved")
      close()
    end
  end })
end

-- ---------------------------------------------------------------- quick tools
function _G.KanborgCompleteCards(arglead)
  local cards = M.ids()
  return vim.tbl_filter(function(c) return c:find(arglead:upper(), 1, true) == 1 end, cards)
end

function _G.KanborgCompleteProjects(arglead)
  local _, projects = M.ids()
  local last = arglead:match("(%S*)$") or ""
  local head = arglead:sub(1, #arglead - #last)
  local out = {}
  for _, p in ipairs(projects) do
    if p:find(last:upper(), 1, true) == 1 then out[#out + 1] = head .. p end
  end
  return out
end

local function after_change(msg)
  M.refresh()
  if msg and msg ~= "" then notify(vim.trim(msg)) end
end

function M.link(ltype, other, id)
  id = id or need_card()
  if not id then return end
  local function go(t, o)
    if not o or o == "" then return end
    local out = run_or_warn({ "link", id, t, o:upper() })
    if out then after_change(out) end
  end
  if ltype and other then return go(ltype, other) end
  vim.ui.select({ "depends", "relates", "parent" }, {
    prompt = id .. " link type",
    format_item = function(t)
      return ({ depends = "depends on (blocked until it is DONE)", relates = "relates to", parent = "parent (sub-card of)" })[t]
    end }, function(t)
    if not t then return end
    vim.ui.input({ prompt = id .. " " .. t .. " → card: ", completion = "customlist,v:lua.KanborgCompleteCards" },
      function(o) go(t, o) end)
  end)
end

function M.unlink(other, id)
  id = id or need_card()
  if not id then return end
  if other then
    local out = run_or_warn({ "unlink", id, other:upper() })
    return out and after_change(out)
  end
  local info = run_or_warn({ "show", id }, true)
  if not info then return end
  local items = {}
  for _, d in ipairs(info.depends_on or {}) do items[#items + 1] = { "depends", d } end
  for _, d in ipairs(info.relates or {}) do items[#items + 1] = { "relates", d } end
  if info.meta and info.meta.parent and info.meta.parent ~= "" then items[#items + 1] = { "parent", info.meta.parent } end
  if #items == 0 then return notify(id .. " has no links") end
  vim.ui.select(items, { prompt = "unlink from " .. id, format_item = function(it) return it[1] .. " " .. it[2] end },
    function(it)
      if not it then return end
      local out = run_or_warn({ "unlink", id, it[1], it[2] })
      if out then after_change(out) end
    end)
end

function M.journal(text, id)
  id = id or need_card()
  if not id then return end
  local function go(t)
    if not t or vim.trim(t) == "" then return end
    local out = run_or_warn({ "journal", id, t, "--who", vim.g.kanborg_user or vim.env.USER or "" })
    if out then after_change(out) end
  end
  if text and text ~= "" then return go(text) end
  vim.ui.input({ prompt = "journal " .. id .. ": " }, go)
end

function M.move(status, id)
  id = id or need_card()
  if not id then return end
  local function go(st)
    if not st then return end
    local out = run_or_warn({ "move", id, st })
    if out then after_change(out) end
  end
  if status then return go(status:upper()) end
  vim.ui.select(STATUSES, { prompt = "move " .. id .. " to" }, go)
end

function M.set(id, pairs_)
  local out = run_or_warn(vim.list_extend({ "set", id }, pairs_))
  if out then after_change(out:match("^[^\n]*")) end
end

function M.new(code, title)
  local status = column_of() or "BACKLOG"
  local _, projects = M.ids()
  local function create(c, t, desc)
    local args = { "new", c:upper(), t or "", "-s", status }
    if desc then vim.list_extend(args, { "-d", desc }) end
    local out = run_or_warn(args)
    if out then
      after_change(out)
      local id = out:match(CARD_PAT)
      if id and not title then M.edit(id) end
    end
  end
  local function with_code(c)
    if not c or vim.trim(c) == "" then return end
    c = vim.trim(c):upper()
    local function with_title(t)
      if t == nil then return end
      if vim.tbl_contains(projects, c) then return create(c, t) end
      vim.ui.input({ prompt = "new project " .. c .. " — description: " }, function(d)
        if d ~= nil then create(c, t, d) end
      end)
    end
    if title then return with_title(title) end
    vim.ui.input({ prompt = "title (" .. c .. ", into " .. status .. "): " }, with_title)
  end
  if code then return with_code(code) end
  vim.ui.input({ prompt = "project code: ", default = project_of(M.card_at()) or "",
    completion = "customlist,v:lua.KanborgCompleteProjects" }, with_code)
end

function M.toggle_check()
  local line = api.nvim_get_current_line()
  local new
  if line:match("^%s*[-*]%s+%[ %]") then
    new = line:gsub("%[ %]", "[x]", 1)
  elseif line:match("^%s*[-*]%s+%[[xX]%]") then
    new = line:gsub("%[[xX]%]", "[ ]", 1)
  else
    new = line:gsub("^(%s*)", "%1- [ ] ", 1)
  end
  api.nvim_set_current_line(new)
end

function M.legacy()
  if M.legacy_tab and api.nvim_tabpage_is_valid(M.legacy_tab) then
    api.nvim_set_current_tabpage(M.legacy_tab)
  else
    vim.cmd("tabfirst")
  end
end

M.HELP = {
  "KANBORG TOOLS  (prefix " .. (vim.g.kanborg_prefix or ",") .. ", cursor on a card: column entry, view line, VIEW pane or card file)",
  "",
  "  e  edit fields: title (whole heading) owner start due progress fpoint tags parent depends relates  (:w saves)",
  "  n  new card (into the cursored column; new project code → asks a description, starts at CODE-0)",
  "  l  link: depends on / relates to / parent      u  unlink",
  "  j  journal entry (dated, under ### 4 JOURNAL)   m  move to a status (checks dependencies)",
  "  x  toggle a [ ]/[x] checklist item (checklists drive progress when no 'progress' is set)",
  "",
  "  o  portfolio overview   b  board      w  swimlanes   r  related projects",
  "  p  progress             d  dependencies (blocked / ready / bottlenecks / cycles)",
  "  t  timeline (gantt)     v  view picker   V  back to the legacy layout",
  "",
  "  ?  this help",
  "",
  "  In a view: <CR> open card · -- peek · <Tab>/<S-Tab> next/prev view · rr refresh · q close",
  "  Legacy keys still work in the columns: --  ---  mmm  bbb  rr",
}

function M.help()
  local buf = api.nvim_create_buf(false, true)
  api.nvim_buf_set_lines(buf, 0, -1, false, M.HELP)
  local w = 0
  for _, l in ipairs(M.HELP) do w = math.max(w, fn.strdisplaywidth(l)) end
  local win = api.nvim_open_win(buf, true, { relative = "editor", width = w + 2, height = #M.HELP,
    row = 2, col = math.floor((vim.o.columns - w) / 2), border = "rounded", style = "minimal", title = " kanborg " })
  vim.keymap.set("n", "q", function() api.nvim_win_close(win, true) end, { buffer = buf, nowait = true })
  vim.keymap.set("n", "<Esc>", function() api.nvim_win_close(win, true) end, { buffer = buf, nowait = true })
end

--- Where the user is on the board (public API).
function M.context(win)
  win = win or api.nvim_get_current_win()
  local buf = api.nvim_win_get_buf(win)
  return { card = M.card_at(win), column = column_of(win), view = vim.b[buf].kanborg_view,
    buffer = api.nvim_buf_get_name(buf), line = api.nvim_get_current_line() }
end

-- ---------------------------------------------------------------- keymaps & commands
local function setup()
  local p = vim.g.kanborg_prefix or ","
  local maps = {
    e = M.edit, n = function() M.new() end, l = function() M.link() end, u = function() M.unlink() end,
    j = function() M.journal() end, m = function() M.move() end, x = M.toggle_check,
    o = function() M.view("overview") end,
    b = function() M.view("board", { project_of(M.card_at()) }) end,
    w = function() M.view("lanes", {}) end,
    r = function() M.view("related") end,
    p = function() M.view("progress", {}) end,
    d = function() M.view("deps", {}) end,
    t = function() M.view("timeline", {}) end,
    v = M.pick_view, V = M.legacy, ["?"] = M.help,
  }
  if not vim.g.kanborg_no_maps then
    for k, f in pairs(maps) do vim.keymap.set("n", p .. k, f, { silent = true, desc = "kanborg " .. k }) end
  end

  local cmd = api.nvim_create_user_command
  cmd("KbView", function(o) M.view(o.fargs[1], vim.list_slice(o.fargs, 2)) end, { nargs = "*",
    complete = function(lead, line)
      if #vim.split(line, "%s+") <= 2 then
        return vim.tbl_filter(function(v) return v:find(lead, 1, true) == 1 end, VIEWS)
      end
      return _G.KanborgCompleteProjects(lead)
    end })
  cmd("KbEdit", function(o) M.edit(o.args ~= "" and o.args:upper() or nil) end,
    { nargs = "?", complete = "customlist,v:lua.KanborgCompleteCards" })
  cmd("KbSet", function(o)
    local args = vim.deepcopy(o.fargs)
    local id = (args[1] and not args[1]:find("=")) and table.remove(args, 1):upper() or need_card()
    if id then M.set(id, args) end
  end, { nargs = "+", complete = "customlist,v:lua.KanborgCompleteCards" })
  cmd("KbLink", function(o) M.link(o.fargs[1], o.fargs[2]) end, { nargs = "*",
    complete = function(lead, line)
      if #vim.split(line, "%s+") <= 2 then return { "depends", "relates", "parent" } end
      return _G.KanborgCompleteCards(lead)
    end })
  cmd("KbUnlink", function(o) M.unlink(o.args ~= "" and o.args or nil) end,
    { nargs = "?", complete = "customlist,v:lua.KanborgCompleteCards" })
  cmd("KbJournal", function(o) M.journal(o.args) end, { nargs = "*" })
  cmd("KbMove", function(o) M.move(o.args ~= "" and o.args or nil) end,
    { nargs = "?", complete = function() return STATUSES end })
  cmd("KbNew", function(o) M.new(o.fargs[1], o.fargs[2] and table.concat(vim.list_slice(o.fargs, 2), " ") or nil) end,
    { nargs = "*", complete = "customlist,v:lua.KanborgCompleteProjects" })
  cmd("KbValidate", function()
    local out = run_or_warn({ "validate" })
    if out then notify(vim.trim(out), out:match("consistent") and vim.log.levels.INFO or vim.log.levels.WARN) end
  end, {})
  cmd("KbRefresh", function() M.refresh() end, {})
end

setup()
package.loaded.kanborg = M
return M
