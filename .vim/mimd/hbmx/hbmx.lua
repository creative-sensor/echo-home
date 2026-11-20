-- hbmx.lua: Neovim frontend of hbmx, the large-file reader & data analyst agent (requires Neovim >= 0.10).
-- Loaded through ~/echo-home/.vim/mimd/vimpage_hbmx. Backend: hbmx.py (same folder), engine: hbmx_core.py/hbmx_ops.py.
--
-- hbmx opens in a tab of its own:
--   ┌─ MAP ─────────────────────┬─ VIEW ───────────────────────────────┐
--   │ the file's map: identity, │ what the last look returned: lines,  │
--   │ regions, landmarks, zones,│ hex, search hits, stats, a function, │
--   │ functions, knowledge      │ a finding (the LLM sees a copy)      │
--   ├───────────────────────────┴─┬───────────────────────────────────┤
--   │  HBMX · app.log   IFACE ●3  │ LLM · raw output                  │  ← tabs: click one (or <Tab>)
--   │ what hbmx does, your input  │ the raw LLM output, streamed as   │
--   │ on the last line            │ it is generated                   │
--   └─────────────────────────────┴───────────────────────────────────┘
-- The HBMX tab is the chat and the LLM stream side by side; the IFACE tab (the network: what hbmx hears, decides,
-- says) takes the whole width. A tab with news you have not seen is marked (•, ●n network lines). Each file has its
-- own chat in HBMX (one conversation per file): opening another file shows its chat, opening it again brings the old
-- one back. The big file itself is never loaded into a buffer.
--
--   :Hbmx [args]           start (backend args from g:hbmx_args) / go to the hbmx tab / back
--   :HbmxOpen [path]       open (probe + map) a file: the path, the file under the cursor, or the current buffer's file
--   :HbmxAsk <text>        ask without the chat (:'<,'>HbmxAsk attaches the selected lines)
--   :HbmxView <line> [n]   show lines in VIEW            :HbmxCancel  :HbmxQuit
--   :InetJoin hbmx / :InetLeave hbmx  (profile g:inet_agents.hbmx in vimpage_inet, optional)
--
-- MAP / VIEW: <CR> on z7, z7.3, r1.2, L3, K2 (finding), Q4 (question), a line number, "line N", a hex row or a
--             function → VIEW (no LLM)
-- VIEW: ] / [ next / previous page · gl line · gt time · gb byte (hex) · m mark a landmark on the line
-- Bottom pane: click a tab (HBMX / IFACE) · <Tab> / <S-Tab> (normal mode) the other tab
-- Everywhere in the tab: q (normal) back to the previous tab · i chat · C-x interrupt · C-c cancel
-- Options: g:hbmx_python, g:hbmx_args, g:hbmx_toggle_key (e.g. '<M-h>'), g:hbmx_map_width (0.42), g:hbmx_chat_height (12),
--          g:hbmx_llm_width (0.5 of the columns: the LLM half of HBMX)

local MODNAME = ...
local M = {}
local api, fn = vim.api, vim.fn

local function native(path)
  if fn.has("win32") == 1 and path then
    local drive, rest = path:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(path) == 0 and fn.filereadable(path) == 0 then path = drive:upper() .. ":/" .. rest end
    path = path:gsub("\\", "/")
  end
  return path
end
local SCRIPT_DIR = native(fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h"))
local NS = api.nvim_create_namespace("hbmx")
local NS_PAINT = api.nvim_create_namespace("hbmx_paint")
local PROMPT = "hbmx ▶ "
local TABS = { "hbmx", "iface" }                       -- the bottom pane's tabs, in order
local TAB_BUF = { hbmx = "chat", iface = "iface" }      -- tab -> the buffer of its main window
local TAB_TITLE = { hbmx = "HBMX", iface = "IFACE" }    -- HBMX also shows the LLM stream ("log") at its right
local S -- session; nil when not running
local refresh_bars -- defined with the tab (append needs it for the unread marks)

local function set_hl()
  local d = function(name, spec) spec.default = true; api.nvim_set_hl(0, name, spec) end
  d("HbmxPrompt", { bg = "#262626", fg = "#e4e4e4", bold = true, ctermbg = 235, ctermfg = 254, cterm = { bold = true } })
  d("HbmxTab", { bg = "#262626", fg = "#a8a8a8", ctermbg = 235, ctermfg = 248 })
  d("HbmxTabUnread", { bg = "#262626", fg = "#c4c408", bold = true, ctermbg = 235, ctermfg = 184, cterm = { bold = true } })
  d("HbmxTabActive", { bg = "#c4c408", fg = "#000000", bold = true, ctermbg = 184, ctermfg = 16, cterm = { bold = true } })
  d("HbmxHead", { link = "Title" })
  d("HbmxId", { link = "Identifier" })
  d("HbmxLnum", { link = "LineNr" })
  d("HbmxBad", { link = "ErrorMsg" })
  d("HbmxWarn", { link = "WarningMsg" })
  d("HbmxBar", { link = "Special" })
  d("HbmxDim", { link = "Comment" })
  -- The headers of MAP, VIEW and the LLM stream each get their own color (a strip + the title badge), in gruvbox's
  -- aqua, purple and blue; the panes themselves keep the colorscheme's background.
  -- Override with  :hi HbmxMapBar guibg=...  / HbmxMapBadge (the groups are defaults, re-applied after a colorscheme change).
  local P = vim.o.background == "light" and {
    Map = { bar = "#cde3d4", badge = "#427b58", on = "#fbf1c7", cbar = 151, cb = 29, con = 230 },
    View = { bar = "#e7cfdc", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cb = 96, con = 230 },
    Llm = { bar = "#cadbea", badge = "#076678", on = "#fbf1c7", cbar = 153, cb = 24, con = 230 },
  } or {
    Map = { bar = "#2a4237", badge = "#8ec07c", on = "#1d2021", cbar = 23, cb = 108, con = 234 },
    View = { bar = "#43304a", badge = "#d3869b", on = "#1d2021", cbar = 53, cb = 175, con = 234 },
    Llm = { bar = "#2a3b48", badge = "#83a598", on = "#1d2021", cbar = 17, cb = 109, con = 234 },
  }
  for name, p in pairs(P) do
    d("Hbmx" .. name .. "Bar", { bg = p.bar, ctermbg = p.cbar })
    d("Hbmx" .. name .. "Badge", { bg = p.badge, fg = p.on, bold = true, ctermbg = p.cb, ctermfg = p.con, cterm = { bold = true } })
  end
end
set_hl()
api.nvim_create_autocmd("ColorScheme", { callback = set_hl })

local NS_PROMPT = api.nvim_create_namespace("hbmx_prompt")
api.nvim_set_decoration_provider(NS_PROMPT, {
  on_win = function(_, _, buf) return S ~= nil and vim.b[buf].hbmx_chat == true end,
  on_line = function(_, _, buf, row)
    if row ~= api.nvim_buf_line_count(buf) - 1 then return end
    local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    if line:sub(1, #PROMPT) == PROMPT then
      api.nvim_buf_set_extmark(buf, NS_PROMPT, row, 0, { end_col = #PROMPT, hl_group = "HbmxPrompt", ephemeral = true, priority = 200 })
    end
  end,
})

local function notify(msg, level) vim.notify("hbmx: " .. msg, level or vim.log.levels.INFO) end
local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function send(msg)
  if S and S.job then fn.chansend(S.job, vim.json.encode(msg) .. "\n") end
end
local function in_tab() return S and S.tab and api.nvim_tabpage_is_valid(S.tab) and api.nvim_get_current_tabpage() == S.tab end
local function nocomma(s) return s and (s:gsub(",", "")) or s end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end

-- ---------------------------------------------------------------- buffers
local function scroll_to_end(buf)
  for _, w in ipairs(api.nvim_list_wins()) do
    if api.nvim_win_get_buf(w) == buf and w ~= api.nvim_get_current_win() then
      pcall(api.nvim_win_set_cursor, w, { api.nvim_buf_line_count(buf), 0 })
    end
  end
end

--- New output on a tab the bottom pane is not showing: mark it (●n for network lines, • otherwise).
local function mark_unread(key, lines)
  local tab = key == "iface" and "iface" or "hbmx" -- the chat and the LLM stream both live in HBMX
  local W = S.wins or {}
  if key == "log" and valid_win(W.llm) then return end
  if key ~= "log" and valid_win(W.bottom) and api.nvim_win_get_buf(W.bottom) == S.bufs[key] then return end
  if tab == "iface" then
    local n = 0
    for _, l in ipairs(lines) do if vim.trim(l) ~= "" then n = n + 1 end end
    if n == 0 then return end
    S.unread.iface = (S.unread.iface or 0) + n
  elseif S.unread[tab] then
    return -- already marked: no need to redraw the bars
  else
    S.unread[tab] = true
  end
  refresh_bars()
end

local function append(key, text, hl, quiet)
  if not S then return end
  local buf = S.bufs[key]
  if not (buf and api.nvim_buf_is_valid(buf)) then return end
  local lines = vim.split(text or "", "\n", { plain = true })
  local at
  if key == "chat" then
    at = api.nvim_buf_line_count(buf) - 1 -- the prompt line stays last
  else
    at = api.nvim_buf_line_count(buf)
    if at == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == "" then at = 0 end
  end
  vim.bo[buf].modifiable = true
  if key == "chat" then
    api.nvim_buf_set_lines(buf, at, at, false, lines)
  else
    api.nvim_buf_set_lines(buf, at, at == 0 and 1 or at, false, lines)
    vim.bo[buf].modifiable = false
  end
  if hl and hl ~= vim.NIL then
    for i = 0, #lines - 1 do
      pcall(api.nvim_buf_set_extmark, buf, NS, at + i, 0, { line_hl_group = hl, priority = 50 })
    end
  end
  scroll_to_end(buf)
  if not quiet then mark_unread(key, lines) end
end

--- Streamed text: continues the last line (the raw LLM output, right half of the HBMX tab).
local function append_raw(key, text)
  local buf = S.bufs[key]
  local parts = vim.split(text or "", "\n", { plain = true })
  local n = api.nvim_buf_line_count(buf)
  local last = api.nvim_buf_get_lines(buf, n - 1, n, false)[1] or ""
  parts[1] = last .. parts[1]
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, n - 1, n, false, parts)
  vim.bo[buf].modifiable = false
  scroll_to_end(buf)
  mark_unread(key, parts)
end

local function clear_chat()
  local buf = S.bufs.chat
  local n = api.nvim_buf_line_count(buf)
  if n > 1 then api.nvim_buf_set_lines(buf, 0, n - 1, false, {}) end
end

--- Highlights of the MAP / VIEW text (extmarks, recomputed on every update).
local function paint(buf)
  api.nvim_buf_clear_namespace(buf, NS_PAINT, 0, -1)
  local lines = api.nvim_buf_get_lines(buf, 0, -1, false)
  local function mark(row, s, e, group)
    pcall(api.nvim_buf_set_extmark, buf, NS_PAINT, row, s - 1, { end_col = e, hl_group = group, priority = 90 })
  end
  for i, l in ipairs(lines) do
    local row = i - 1
    if l:sub(1, 1) == "#" then
      mark(row, 1, #l, "HbmxHead")
    elseif l:match("^%a[%w ,()._:%-]*:$") or l:match("^%a+[%w ]*%(.*%):$") or l:match("^identity:") or l:match("^structure:") then
      mark(row, 1, #l, "HbmxHead")
    end
    local s, e = l:find("^%s*[%d,]+│")
    if not s then s, e = l:find("^▶[%d,]+│") end
    if s then mark(row, s, e, "HbmxLnum") end
    s, e = l:find("^%x+: ")
    if s and l:match("^%x%x%x%x%x%x%x%x") then mark(row, s, e, "HbmxLnum") end
    for _, pat in ipairs({ "%f[%w]z%d+[%.%d]*", "%f[%w]r%d+[%.%d]*", "%f[%w]L%d+%f[%W]", "%f[%w][KQ]%d+%f[%W]" }) do
      local from = 1
      while true do
        local a, b = l:find(pat, from)
        if not a then break end
        if l:sub(b + 1, b + 1) ~= "_" then mark(row, a, b, "HbmxId") end -- not Q4_0 (a quant type)
        from = b + 1
      end
    end
    for _, w in ipairs({ "ERROR", "FATAL", "CRIT", "critical", "high", "5xx" }) do
      local a, b = l:find("%f[%w]" .. w .. "%f[%W]")
      if a then mark(row, a, b, "HbmxBad") end
    end
    for _, w in ipairs({ "WARN", "medium", "4xx", "SAMPLE", "▲" }) do
      local a, b = l:find("%f[%w]" .. w .. "%f[%W]")
      if not a and w == "▲" then a, b = l:find("▲", 1, true) end
      if a then mark(row, a, b, "HbmxWarn") end
    end
    local a = l:find("\226\150[\129-\143]")
    if a then
      local b = a
      while l:sub(b + 3, b + 5):match("^\226\150[\129-\143]") do b = b + 3 end
      mark(row, a, b + 2, "HbmxBar")
    end
  end
end

local function footer()
  local st = S and S.state or {}
  local parts = {}
  parts[#parts + 1] = st.busy and ("● " .. tostring(st.phase or "busy")) or (S and S.job and "○ idle" or "✖ stopped")
  if (st.queue or 0) > 0 then parts[#parts + 1] = st.queue .. " queued" end
  local net = type(st.net) == "table" and st.net or {}
  if net.joined then
    parts[#parts + 1] = "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
      .. " [" .. tostring(net.state) .. "]" .. (net.members and (" · " .. net.members .. " members") or "")
  else
    parts[#parts + 1] = "🌐 off"
  end
  if st.auto then parts[#parts + 1] = "AUTO" end
  if st.approval and st.approval ~= vim.NIL then parts[#parts + 1] = "❓ y/n/a" end
  return table.concat(parts, " · ")
end

--- The bottom window's bar: the tabs (clickable: %N@v:lua.HbmxTabClick@ passes the tab index), then the status.
local function tabs_bar()
  local parts = {}
  for i, key in ipairs(TABS) do
    local label = TAB_TITLE[key]
    if key == "hbmx" and S.chat_name then label = label .. " · " .. S.chat_name end
    local u, hl = S.unread[key], "HbmxTab"
    if key == S.sel then
      hl = "HbmxTabActive"
    elseif key == "iface" and (u or 0) > 0 then
      hl, label = "HbmxTabUnread", label .. " ●" .. u
    elseif key ~= "iface" and u then
      hl, label = "HbmxTabUnread", label .. " •"
    end
    parts[#parts + 1] = "%" .. i .. "@v:lua.HbmxTabClick@%#" .. hl .. "# " .. esc(label) .. " %X%#HbmxDim# "
  end
  return table.concat(parts) .. " " .. esc(footer()) .. "%*"
end

--- A MAP / VIEW bar: the pane's badge in its color, then the rest of the title ("MAP · app.log · csv").
local function pane_bar(title, badge, name)
  title = title or badge
  local rest = title:match("^%u+%s*·%s*(.*)$") or (title ~= badge and title or "")
  return "%#Hbmx" .. name .. "Badge# " .. badge .. " %#Hbmx" .. name .. "Bar# " .. esc(rest)
end

--- Only the header is colored: the window's bar (focused or not) takes the pane's strip color.
local function pane_colors(w, name)
  vim.wo[w].winhighlight = ("WinBar:Hbmx%sBar,WinBarNC:Hbmx%sBar"):gsub("%%s", name)
end

function refresh_bars()
  if not S then return end
  local W = S.wins
  if valid_win(W.map) then vim.wo[W.map].winbar = pane_bar(S.titles.map, "MAP", "Map") end
  if valid_win(W.view) then vim.wo[W.view].winbar = pane_bar(S.titles.view, "VIEW", "View") end
  if valid_win(W.bottom) then vim.wo[W.bottom].winbar = tabs_bar() end
  if valid_win(W.llm) then
    local model = (S.state or {}).model
    vim.wo[W.llm].winbar = pane_bar("LLM · raw output, streamed"
      .. (model and model ~= vim.NIL and (" · " .. vim.fn.fnamemodify(tostring(model), ":t")) or ""), "LLM", "Llm")
  end
end

local function set_pane(key, title, text)
  local buf = S.bufs[key]
  local lines = vim.split(text or "", "\n", { plain = true })
  local keep = key == "map" and S.titles.map == title and valid_win(S.wins.map) and api.nvim_win_get_cursor(S.wins.map) or nil
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  paint(buf)
  S.titles[key] = title
  local w = S.wins[key]
  if valid_win(w) then
    if keep then
      pcall(api.nvim_win_set_cursor, w, { math.min(keep[1], #lines), 0 })
    else
      pcall(api.nvim_win_set_cursor, w, { 1, 0 })
    end
  end
  refresh_bars()
end

-- ---------------------------------------------------------------- the tab
local function sizes()
  if not S then return end
  local W = S.wins
  if valid_win(W.map) then pcall(api.nvim_win_set_width, W.map, math.floor(vim.o.columns * (vim.g.hbmx_map_width or 0.42))) end
  if valid_win(W.bottom) then pcall(api.nvim_win_set_height, W.bottom, vim.g.hbmx_chat_height or 12) end
  if valid_win(W.llm) then pcall(api.nvim_win_set_width, W.llm, math.floor(vim.o.columns * (vim.g.hbmx_llm_width or 0.5))) end
end

local function win_opts(w, wrap)
  vim.wo[w].wrap = wrap
  vim.wo[w].number = false
  vim.wo[w].relativenumber = false
  vim.wo[w].signcolumn = "no"
  vim.wo[w].foldcolumn = "0"
  vim.wo[w].cursorline = not wrap
  vim.wo[w].list = false
end

local function win_in_tab(w) return valid_win(w) and api.nvim_win_get_tabpage(w) == S.tab end

local function layout()
  if not (S.tab and api.nvim_tabpage_is_valid(S.tab)) then
    vim.cmd("tabnew")
    S.tab = api.nvim_get_current_tabpage()
    local scratch = api.nvim_get_current_buf()
    S.wins = { map = api.nvim_get_current_win() }
    api.nvim_win_set_buf(S.wins.map, S.bufs.map)
    if scratch ~= S.bufs.map and api.nvim_buf_get_name(scratch) == "" then pcall(api.nvim_buf_delete, scratch, { force = true }) end
  end
  api.nvim_set_current_tabpage(S.tab)
  local W = S.wins
  if not win_in_tab(W.map) then
    local anchor = win_in_tab(W.view) and W.view or api.nvim_get_current_win()
    api.nvim_set_current_win(anchor)
    vim.cmd("topleft vsplit")
    W.map = api.nvim_get_current_win()
    api.nvim_win_set_buf(W.map, S.bufs.map)
  end
  if not win_in_tab(W.view) then
    api.nvim_set_current_win(W.map)
    vim.cmd("rightbelow vsplit")
    W.view = api.nvim_get_current_win()
    api.nvim_win_set_buf(W.view, S.bufs.view)
  end
  if not win_in_tab(W.bottom) then -- the tabs' main window: the chat (HBMX) or the network (IFACE)
    W.bottom = api.nvim_open_win(S.bufs[TAB_BUF[S.sel]], false, { split = "below", win = -1 })
  end
  if S.sel == "hbmx" then -- HBMX: the chat | the LLM stream, side by side
    if not win_in_tab(W.llm) then
      W.llm = api.nvim_open_win(S.bufs.log, false, { split = "right", win = W.bottom })
      pcall(api.nvim_win_set_cursor, W.llm, { api.nvim_buf_line_count(S.bufs.log), 0 })
    end
    win_opts(W.llm, true)
    pane_colors(W.llm, "Llm")
    vim.wo[W.llm].winfixheight = true
  elseif valid_win(W.llm) then -- IFACE takes the whole width
    pcall(api.nvim_win_close, W.llm, true)
    W.llm = nil
  end
  win_opts(W.map, false)
  win_opts(W.view, false)
  pane_colors(W.map, "Map")
  pane_colors(W.view, "View")
  win_opts(W.bottom, true)
  vim.wo[W.bottom].winfixheight = true
  sizes()
  refresh_bars()
end

--- Show a tab of the bottom pane (1-2 or "hbmx" / "iface") and focus its main window.
function M.select_tab(which)
  if not S then return end
  local key = type(which) == "number" and TABS[which] or which
  if not TAB_BUF[key] then return end
  S.sel = key
  S.unread[key] = key == "iface" and 0 or false
  layout()
  local w, buf = S.wins.bottom, S.bufs[TAB_BUF[key]]
  if api.nvim_win_get_buf(w) ~= buf then api.nvim_win_set_buf(w, buf) end
  win_opts(w, true)
  pcall(api.nvim_win_set_cursor, w, { api.nvim_buf_line_count(buf), 0 })
  api.nvim_set_current_win(w)
  if key == "hbmx" then vim.cmd("startinsert!") else vim.cmd("stopinsert") end
  refresh_bars()
end

function _G.HbmxTabClick(minwid) M.select_tab(minwid) end

local function cycle_tab(delta)
  local i = 1
  for k, key in ipairs(TABS) do if key == S.sel then i = k end end
  M.select_tab((i - 1 + delta) % #TABS + 1)
end

function M.focus_chat()
  if S then M.select_tab("hbmx") end
end

--- Clicking out of the chat input (into MAP, VIEW, another tab) leaves insert mode.
local function on_win_enter()
  if not (S and in_tab()) then return end
  local cur = api.nvim_get_current_win()
  if not (cur == S.wins.bottom and S.sel == "hbmx") and fn.mode() == "i" then vim.cmd("stopinsert") end
end

function M.back()
  if S and S.prev_tab and api.nvim_tabpage_is_valid(S.prev_tab) and S.prev_tab ~= S.tab then
    vim.cmd("stopinsert")
    api.nvim_set_current_tabpage(S.prev_tab)
  end
end

function M.show()
  if not S then return M.start() end
  if not in_tab() then S.prev_tab = api.nvim_get_current_tabpage(); S.anchor = api.nvim_get_current_win() end
  layout()
  M.focus_chat()
end

function M.toggle()
  if not S then return M.start() end
  if in_tab() then return M.back() end
  M.show()
end

-- ---------------------------------------------------------------- navigation (MAP / VIEW → engine, no LLM)
local function view_count()
  return valid_win(S.wins.view) and math.max(10, api.nvim_win_get_height(S.wins.view) - 3) or 40
end

function M.nav(msg)
  if not (S and S.job) then return notify("not running (:Hbmx)", vim.log.levels.WARN) end
  msg.t = "nav"
  msg.count = msg.count or view_count()
  send(msg)
end

local function section_of(buf, row)
  for r = row, 1, -1 do
    local l = api.nvim_buf_get_lines(buf, r - 1, r, false)[1] or ""
    if l:match("^%a") then return l end
  end
  return ""
end

--- What the cursor line points at, as a nav message.
local function ref_at(buf, row)
  local l = api.nvim_buf_get_lines(buf, row - 1, row, false)[1] or ""
  local hex = l:match("^(%x%x%x%x%x%x%x%x+): ")
  if hex then return { op = "byte", arg = tonumber(hex, 16), hex = true } end
  local n = l:match("^%s*([%d,]+)│") or l:match("^▶([%d,]+)│")
  if n then return { op = "line", arg = nocomma(n), center = true } end
  local id = l:match("^%s*(L%d+)%s")
  if id then return { op = "landmark", arg = id } end
  id = l:match("^%s*([KQ]%d+)%s")
  if id then return { op = "knowledge", arg = id } end
  id = l:match("^%s*(z%d+[%.%d]*)%s")
  if id then return { op = "zone", arg = id } end
  id = l:match("^%s*▾%s+(r[%d%.]*%d)%s") or l:match("^%s*•%s+(r[%d%.]*%d)%s")
  if id then return { op = "region", arg = id } end
  if section_of(buf, row):match("^functions") then
    local name = l:match("^%s%s([%w_%-]+)%s")
    if name then return { op = "function", arg = name } end
  end
  if section_of(buf, row):match("^knowledge") then return { op = "knowledge" } end
  n = l:match("%f[%w]line ([%d,]+)")
  if n then return { op = "line", arg = nocomma(n), center = true } end
  n = l:match("@byte ([%d,]+)")
  if n then return { op = "byte", arg = nocomma(n) } end
  id = l:match("%f[%w]([KQ]%d+)[%s:,%)]")
  if id then return { op = "knowledge", arg = id } end
  id = l:match("%f[%w](z%d+[%.%d]*)%f[^%w%.]") or l:match("%f[%w](r%d+[%.%d]*)%f[^%w%.]") or l:match("%f[%w](L%d+)%f[%W]")
  if id then return { op = id:sub(1, 1) == "z" and "zone" or id:sub(1, 1) == "r" and "region" or "landmark", arg = id } end
  return nil
end

local function follow()
  local buf = api.nvim_get_current_buf()
  local m = ref_at(buf, api.nvim_win_get_cursor(0)[1])
  if not m then return notify("nothing to follow on this line (zone, region, landmark, finding, line number, function)") end
  M.nav(m)
end

local function view_line_under_cursor()
  local l = api.nvim_get_current_line()
  local n = l:match("^%s*([%d,]+)│") or l:match("^▶([%d,]+)│") or l:match("%f[%w]line ([%d,]+)")
  return n and tonumber(nocomma(n)) or nil
end

local function mark_here()
  local n = view_line_under_cursor()
  if not n then return notify("put the cursor on a numbered line of the file") end
  vim.ui.input({ prompt = "landmark at line " .. n .. ": " }, function(label)
    if label and vim.trim(label) ~= "" then M.nav({ op = "mark", arg = n, label = label }) end
  end)
end

local function ask_nav(op, prompt)
  vim.ui.input({ prompt = prompt }, function(v)
    if v and vim.trim(v) ~= "" then
      M.nav({ op = op, arg = op == "time" and vim.trim(v) or nocomma(vim.trim(v)), hex = op == "byte" or nil })
    end
  end)
end

-- ---------------------------------------------------------------- input & buffers
local function context()
  local pane = S.last_pane
  local w = pane and S.wins[pane]
  if valid_win(w) then
    local row = api.nvim_win_get_cursor(w)[1]
    return { pane = pane, line = row, text = api.nvim_buf_get_lines(S.bufs[pane], row - 1, row, false)[1] or "" }
  end
  return nil
end

local function submit(text, sel)
  text = vim.trim(text or "")
  if text == "" then return end
  if not (S and S.job) then return notify("not running (:Hbmx)", vim.log.levels.WARN) end
  send({ t = "input", text = text, ctx = context(), sel = sel })
end

local function keys(b, key)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "q", M.back, o)
  vim.keymap.set({ "n", "i" }, "<C-x>", function() send({ t = "interrupt" }) end, o)
  vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
  if key ~= "chat" then
    vim.keymap.set("n", "i", M.focus_chat, o)
    vim.keymap.set("n", "a", M.focus_chat, o)
  end
  if key == "chat" or key == "iface" or key == "log" then
    vim.keymap.set("n", "<Tab>", function() cycle_tab(1) end, o)
    vim.keymap.set("n", "<S-Tab>", function() cycle_tab(-1) end, o)
  end
  if key == "map" or key == "view" then
    vim.keymap.set("n", "<CR>", follow, o)
    vim.keymap.set("n", "r", function() M.nav({ op = "map" }) end, o)
    api.nvim_create_autocmd("BufEnter", { buffer = b, callback = function() if S then S.last_pane = key end end })
  end
  if key == "view" then
    vim.keymap.set("n", "]", function() M.nav({ op = "next" }) end, o)
    vim.keymap.set("n", "[", function() M.nav({ op = "prev" }) end, o)
    vim.keymap.set("n", "gl", function() ask_nav("line", "line: ") end, o)
    vim.keymap.set("n", "gt", function() ask_nav("time", "time (2026-01-02T10:00): ") end, o)
    vim.keymap.set("n", "gb", function() ask_nav("byte", "byte offset: ") end, o)
    vim.keymap.set("n", "m", mark_here, o)
  end
end

--- A chat buffer for one session (one per file; "_" is the one before any file is open).
local function new_chat(key, name)
  local chat = api.nvim_create_buf(false, true)
  vim.bo[chat].buftype = "prompt"
  vim.bo[chat].bufhidden = "hide"
  vim.bo[chat].filetype = "hbmxchat"
  vim.b[chat].hbmx_chat = true
  pcall(api.nvim_buf_set_name, chat, key == "_" and "hbmx://chat" or ("hbmx://chat/" .. (name or key) .. "@" .. key:sub(1, 8)))
  fn.prompt_setprompt(chat, PROMPT)
  fn.prompt_setcallback(chat, function(text)
    vim.schedule(function() -- the prompt buffer keeps the typed line; the backend echoes it instead
      local n = api.nvim_buf_line_count(chat)
      for i = n - 1, math.max(1, n - 40), -1 do
        if api.nvim_buf_get_lines(chat, i - 1, i, false)[1] == PROMPT .. text then
          api.nvim_buf_set_lines(chat, i - 1, i, false, {})
          break
        end
      end
    end)
    submit(text)
  end)
  fn.prompt_setinterrupt(chat, function() send({ t = "cancel" }) end)
  keys(chat, "chat")
  S.chats[key] = chat
  return chat
end

--- The backend moved to another file's session: its chat becomes the one in the HBMX tab.
local function switch_chat(key, name)
  local buf = S.chats[key]
  if not (buf and api.nvim_buf_is_valid(buf)) then buf = new_chat(key, name) end
  local typing = valid_win(S.wins.bottom) and api.nvim_get_current_win() == S.wins.bottom and fn.mode() == "i"
  S.bufs.chat, S.chat_name = buf, name
  if S.sel == "hbmx" and valid_win(S.wins.bottom) then
    api.nvim_win_set_buf(S.wins.bottom, buf)
    win_opts(S.wins.bottom, true)
    pcall(api.nvim_win_set_cursor, S.wins.bottom, { api.nvim_buf_line_count(buf), 0 })
    if typing then vim.cmd("startinsert!") end
  end
  refresh_bars()
end

local function create_buffers()
  local function scratch(name, ft)
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].filetype = ft
    vim.bo[b].modifiable = false
    pcall(api.nvim_buf_set_name, b, "hbmx://" .. name)
    return b
  end
  local bufs = { chat = new_chat("_", nil), iface = scratch("iface", "hbmxiface"), log = scratch("llm", "hbmxlog"),
    map = scratch("map", "hbmxmap"), view = scratch("view", "hbmxview") }
  for key, b in pairs(bufs) do
    if key ~= "chat" then keys(b, key) end
  end
  return bufs
end

-- ---------------------------------------------------------------- backend messages
local function on_message(msg)
  local t = msg.t
  if t == "w" then
    local b = (msg.b == "iface" or msg.b == "log") and msg.b or "chat"
    if msg.raw then append_raw(b, msg.text) else append(b, msg.text, msg.hl ~= vim.NIL and msg.hl or nil, msg.quiet == true) end
  elseif t == "pane" then
    set_pane(msg.b == "map" and "map" or "view", msg.title, msg.text)
  elseif t == "session" then
    switch_chat(tostring(msg.key), msg.name)
  elseif t == "state" then
    S.state = msg
    refresh_bars()
  elseif t == "show" then
    if in_tab() then M.focus_chat() else notify("needs your answer (:Hbmx)", vim.log.levels.WARN) end
  elseif t == "notify" then
    if not in_tab() then notify(msg.text, msg.level == "warn" and vim.log.levels.WARN or vim.log.levels.INFO) end
  elseif t == "clear" then
    clear_chat()
  elseif t == "exit" then
    append("chat", "backend exited", "Comment")
  end
end

local function on_stdout(_, data)
  if not S then return end
  data[1] = (S.partial or "") .. data[1]
  S.partial = table.remove(data)
  for _, line in ipairs(data) do
    if line ~= "" then
      local ok, msg = pcall(vim.json.decode, line)
      if ok and type(msg) == "table" then
        local ok2, err = pcall(on_message, msg)
        if not ok2 then append("log", "frontend error: " .. tostring(err), "ErrorMsg") end
      end
    end
  end
end

local function on_stderr(_, data)
  if not S then return end
  for _, l in ipairs(data) do
    if l ~= "" then append("log", l, "ErrorMsg") end
  end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  append("chat", "hbmx stopped (exit " .. tostring(code) .. "). :Hbmx restarts it.", code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh_bars()
end

-- ---------------------------------------------------------------- lifecycle
local function normalize_args(args)
  args = args or vim.g.hbmx_args or {}
  if type(args) == "string" then args = vim.split(args, "%s+", { trimempty = true }) end
  return vim.deepcopy(args)
end

function M.start(args, open_path)
  if S and S.job then return M.show() end
  args = normalize_args(args)
  local py = vim.g.hbmx_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", SCRIPT_DIR .. "/hbmx.py", "--cwd", native(fn.getcwd()) }
  vim.list_extend(cmd, args)
  if open_path then vim.list_extend(cmd, { "--open", open_path }) end
  local old = S
  S = { state = {}, wins = old and old.wins or {}, tab = old and old.tab, titles = {}, sel = "hbmx",
    unread = { hbmx = false, iface = 0 }, chats = old and old.chats or {}, chat_name = old and old.chat_name,
    prev_tab = old and old.prev_tab or api.nvim_get_current_tabpage(), anchor = api.nvim_get_current_win() }
  S.bufs = old and old.bufs or create_buffers()
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    return notify("cannot start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
  end
  M.show()
end

--- The path to open: the argument, the file under the cursor, or the current buffer's file.
local function pick_path(arg)
  if arg and vim.trim(arg) ~= "" then return native(fn.fnamemodify(fn.expand(vim.trim(arg)), ":p")) end
  if in_tab() then return nil end
  local cf = fn.expand("<cfile>")
  if cf ~= "" then
    local dir = vim.b.netrw_curdir
    local p = dir and (dir .. "/" .. cf) or fn.fnamemodify(cf, ":p")
    if fn.filereadable(p) == 1 then return native(p) end
  end
  local cur = api.nvim_buf_get_name(0)
  if cur ~= "" and fn.filereadable(cur) == 1 then return native(cur) end
  return nil
end

function M.open(arg)
  local path = pick_path(arg)
  local function go(p)
    if not p or p == "" then return end
    p = native(fn.fnamemodify(fn.expand(p), ":p"))
    if fn.filereadable(p) == 0 then return notify("not a readable file: " .. p, vim.log.levels.WARN) end
    if not (S and S.job) then return M.start(nil, p) end
    send({ t = "open", path = p })
    M.show()
  end
  if path then return go(path) end
  vim.ui.input({ prompt = "hbmx open: ", completion = "file" }, go)
end

function M.ask(text, range)
  local sel
  if range then
    local lines = api.nvim_buf_get_lines(0, range[1] - 1, range[2], false)
    sel = { file = api.nvim_buf_get_name(0), start = range[1], ["end"] = range[2], text = table.concat(lines, "\n") }
  end
  if not (S and S.job) then
    M.start()
    vim.defer_fn(function() submit(text, sel) end, 800)
    return
  end
  submit(text, sel)
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) and #api.nvim_list_tabpages() > 1 then
    pcall(api.nvim_set_current_tabpage, S.tab)
    pcall(vim.cmd, "tabclose")
  end
  for _, group in ipairs({ S.bufs or {}, S.chats or {} }) do
    for _, b in pairs(group) do
      if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
    end
  end
  S = nil
end

function M.status() return S and S.job and S.state.net or nil end

local cmd = api.nvim_create_user_command
cmd("Hbmx", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start hbmx / go to its tab / back" })
cmd("HbmxOpen", function(o) M.open(o.args) end, { nargs = "?", complete = "file", desc = "hbmx: open (probe + map) a file" })
cmd("HbmxAsk", function(o) M.ask(o.args, o.range > 0 and { o.line1, o.line2 } or nil) end,
  { nargs = "*", range = true, desc = "Ask hbmx (a range attaches the lines)" })
cmd("HbmxView", function(o)
  M.nav({ op = "line", arg = nocomma(o.fargs[1]), count = tonumber(o.fargs[2]) })
  M.show()
end, { nargs = "+", desc = "hbmx: show lines of the current file in VIEW" })
cmd("HbmxCancel", function() send({ t = "cancel" }) end, {})
cmd("HbmxQuit", M.quit, {})

if vim.g.hbmx_toggle_key then
  vim.keymap.set({ "n", "i", "t" }, vim.g.hbmx_toggle_key, function()
    if fn.mode() == "t" or fn.mode() == "i" then vim.cmd("stopinsert") end
    M.toggle()
  end, { silent = true, desc = "hbmx tab ⇄ previous tab" })
end

local aug = api.nvim_create_augroup("hbmx", { clear = true })
api.nvim_create_autocmd("VimResized", { group = aug, callback = function() if S and in_tab() then sizes() end end })
api.nvim_create_autocmd("WinEnter", { group = aug, callback = on_win_enter })
api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function() if S and S.job then send({ t = "quit" }) end end })
api.nvim_create_autocmd("TabClosed", { group = aug, callback = function()
  if S and S.tab and not api.nvim_tabpage_is_valid(S.tab) then S.tab, S.wins = nil, {} end
end })

package.loaded.hbmx = M

-- ---------------------------------------------------------------- Intelligence Network
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("hbmx", {
      join = function(p)
        if S and S.job then
          send({ t = "net", op = "join", name = p.name, purpose = p.purpose ~= "" and p.purpose or nil,
            hub = p.hub, network = p.network.name })
        else
          M.start(vim.list_extend(normalize_args(nil), inet.agent_args(p)))
        end
      end,
      leave = function() if S and S.job then send({ t = "net", op = "leave" }) end end,
      status = function() return M.status() end,
    })
  elseif not ok then
    vim.notify("hbmx: Intelligence Network driver not loaded (" .. dir .. "/inet.lua): " .. tostring(inet), vim.log.levels.WARN)
  end
end

-- `:source hbmx.lua` opens hbmx unless g:hbmx_no_autostart is set (vimpage_hbmx sets it).
if MODNAME == nil and not vim.g.hbmx_no_autostart then M.start() end

return M
