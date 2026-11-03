-- memphia.lua: Neovim frontend for the Memphia agentic shell (requires Neovim >= 0.10).
--
-- The agent runs in memphia_nvim.py (next to this file) on top of memphia_core.py; this
-- script only draws the UI, so long LLM streams and command output never block the editor.
--
--   :source /path/to/memphia.lua      open the UI in a new tab (backend args from vim.g.memphia_args)
--   :Memphia [--port 8080 ...]        open/focus the UI, starting the backend with these args
--   :MemphiaQuit                      stop the backend and close the UI tab
--
-- Options: vim.g.memphia_python (default "python3"), vim.g.memphia_args (list or string).
--
-- Layout (same as the TUI)
--   AGENT pane | right pane (LLM / STDOUT / STDERR / UVIAN / MAP-REDUCE / GRAPH)
--   queue panel (steering messages and queued tasks, only while non-empty)
--   status bar + input line (always active, even while the agent is running)
--
-- Output panes are ordinary read-only buffers: scroll with the wheel, <C-u>/<C-d>, gg/G,
-- search with /, yank text, etc. A pane follows new output while its last line is visible.

local MODNAME = ... -- set when loaded with require(), nil when :source'd
local M = {}
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

local SCRIPT_DIR = fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
local RIGHT_TABS = { "llm", "stdout", "stderr", "uvian", "mapreduce", "graph" }
local TITLES = {
  agent = "AGENT", llm = "LLM", stdout = "STDOUT", stderr = "STDERR",
  uvian = "UVIAN", mapreduce = "MAP-REDUCE", graph = "GRAPH",
}
local MAX_LINES = 5000
local WHEEL_LINES = 3
local HISTORY_FILE = ".memphia.py.history" -- shared with the prompt_toolkit TUI
local NS = api.nvim_create_namespace("memphia")
local NS_PROMPT = api.nvim_create_namespace("memphia_prompt")
local SPINNER = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }

local S -- session state; nil when nothing is running

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    MemphiaHeader = { bg = "#1c1c1c", fg = "#888888" },
    MemphiaTab = { bg = "#262626", fg = "#a8a8a8" },
    MemphiaTabUnread = { bg = "#262626", fg = "#c4c408", bold = true },
    MemphiaTabActive = { bg = "#c4c408", fg = "#000000", bold = true },
    MemphiaQueue = { bg = "#121212" },
    MemphiaQSteer = { bg = "#5f00af", fg = "#ffffff", bold = true },
    MemphiaQTask = { bg = "#005f87", fg = "#ffffff", bold = true },
    MemphiaQMore = { fg = "#888888", italic = true },
    MemphiaStatus = { bg = "#303030", fg = "#d0d0d0" },
    MemphiaStIdle = { bg = "#005f00", fg = "#ffffff", bold = true },
    MemphiaStBusy = { bg = "#af5f00", fg = "#ffffff", bold = true },
    MemphiaStOn = { bg = "#5f00af", fg = "#ffffff" },
    MemphiaStOff = { bg = "#444444", fg = "#999999" },
    MemphiaLlm = { bg = "#c4c408", fg = "#000000", bold = true },
    MemphiaPSteer = { bg = "#5f00af", fg = "#ffffff", bold = true },
    MemphiaPHint = { fg = "#808080" },
    MemphiaPApprove = { bg = "#af0000", fg = "#ffffff", bold = true },
  }
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
end

-- ---------------------------------------------------------------- helpers
local function valid_win(win) return win and api.nvim_win_is_valid(win) end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end -- for statusline strings
local function oneline(s) return (tostring(s or ""):gsub("[\r\n]", " ")) end

local function send(msg)
  if S and S.job then pcall(fn.chansend, S.job, vim.json.encode(msg) .. "\n") end
end

local function right_key() return RIGHT_TABS[S.right] end

local function key_of_win(win)
  if win == S.wins.agent then return "agent" end
  if win == S.wins.right then return right_key() end
end

local function last_visible(win)
  return api.nvim_win_call(win, function() return fn.line("w$") >= fn.line("$") end)
end

local function scroll_win(win, keys)
  if not valid_win(win) then return end
  api.nvim_win_call(win, function() vim.cmd("normal! " .. api.nvim_replace_termcodes(keys, true, false, true)) end)
end

local function scroll_target_win()
  if S.target == "agent" and valid_win(S.wins.agent) then return S.wins.agent end
  return S.wins.right
end

-- ---------------------------------------------------------------- bars
local refresh_bars

local function prompt_spec()
  local st = S.state
  if st.approval then
    if st.approval.editing then return { { " EDIT CMD ", "MemphiaPApprove" }, { " ", nil } } end
    return { { " ALLOW? y/n/e ", "MemphiaPApprove" }, { " ", nil } }
  end
  if st.busy then
    return { { " STEER ", "MemphiaPSteer" }, { " !now +task ", "MemphiaPHint" }, { " ", nil } }
  end
  return { { " MEMPHIA ", "MemphiaLlm" }, { " ❯ ", "MemphiaPHint" } }
end

local function update_prompt()
  local spec = prompt_spec()
  local text = ""
  for _, p in ipairs(spec) do text = text .. p[1] end
  local ib = S.bufs.input
  local old = S.prompt
  if text ~= old then
    fn.prompt_setprompt(ib, text)
    local n = api.nvim_buf_line_count(ib)
    local line = api.nvim_buf_get_lines(ib, n - 1, n, false)[1]
    if line:sub(1, #old) == old then
      api.nvim_buf_set_text(ib, n - 1, 0, n - 1, #old, { text })
    elseif line:sub(1, #text) ~= text then
      api.nvim_buf_set_lines(ib, n - 1, n, false, { text })
    end
    S.prompt = text
  end
  api.nvim_buf_clear_namespace(ib, NS_PROMPT, 0, -1)
  local n, col = api.nvim_buf_line_count(ib), 0
  for _, p in ipairs(spec) do
    if p[2] then
      api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, col, { end_col = col + #p[1], hl_group = p[2] })
    end
    col = col + #p[1]
  end
end

local function status_bar()
  local st = S.state
  local spin = st.busy and SPINNER[math.floor(uv.hrtime() / 2.5e8) % #SPINNER + 1] or "●"
  return table.concat({
    st.busy and "%#MemphiaStBusy#" or "%#MemphiaStIdle#", " ", spin, " ", esc(st.phase or "starting"), " ",
    "%#MemphiaStatus# ", esc(st.model or "-"), " ",
    st.graph and "%#MemphiaStOn# graph " or "%#MemphiaStOff# graph off ",
    "%#MemphiaStatus# 📌 ", esc(st.pinned or "-"), " │ cwd ", esc(st.cwd or ""), " │ ===h help %=",
    S.job and "" or "%#MemphiaPApprove# backend stopped ",
  })
end

function refresh_bars()
  if not S then return end
  local w = S.wins
  if valid_win(w.agent) then
    vim.wo[w.agent].winbar = "%#MemphiaTabActive# AGENT %#MemphiaHeader#"
      .. (S.follow.agent and "" or "  ⏸ scrolled") .. (S.target == "agent" and " ◀" or "")
  end
  if valid_win(w.right) then
    local parts = {}
    for i, key in ipairs(RIGHT_TABS) do
      local label
      if i == S.right then
        label = "%#MemphiaTabActive# F" .. i .. " " .. TITLES[key] .. " "
      elseif S.unread[key] then
        label = "%#MemphiaTabUnread# F" .. i .. " " .. TITLES[key] .. "• "
      else
        label = "%#MemphiaTab# F" .. i .. " " .. TITLES[key] .. " "
      end
      parts[#parts + 1] = "%" .. i .. "@v:lua.MemphiaTabClick@" .. label .. "%X%#MemphiaHeader# "
    end
    local rk = right_key()
    vim.wo[w.right].winbar = table.concat(parts) .. (S.follow[rk] and "" or " ⏸ scrolled")
      .. (S.target == "right" and " ◀" or "")
  end
  if valid_win(w.input) then vim.wo[w.input].winbar = status_bar() end
end

-- winbar click handler: %N@v:lua.MemphiaTabClick@ passes the tab index as minwid
function _G.MemphiaTabClick(minwid) M.select_tab(minwid) end

-- ---------------------------------------------------------------- output buffers
local function append(key, segs)
  local buf = S.bufs[key]
  local n = api.nvim_buf_line_count(buf)
  local cur = api.nvim_buf_get_lines(buf, n - 1, n, false)[1]
  local base, replace_from = #cur, #cur -- byte col where new text starts on the existing last line
  local lines, hls = { "" }, {}
  for _, seg in ipairs(segs) do
    local parts = vim.split(seg[1], "\n", { plain = true })
    for i, part in ipairs(parts) do
      if i > 1 then lines[#lines + 1] = "" end
      if part:find("\r", 1, true) then -- progress bars rewrite the line: keep the latest state
        local keep
        for piece in part:gmatch("[^\r]+") do keep = piece end
        part = keep or ""
        lines[#lines] = ""
        if #lines == 1 then base, replace_from = 0, 0 end
        for j = #hls, 1, -1 do
          if hls[j][1] == #lines then table.remove(hls, j) end
        end
      end
      if #part > 0 then
        local s = #lines[#lines]
        lines[#lines] = lines[#lines] .. part
        if seg[2] ~= 0 then hls[#hls + 1] = { #lines, s, s + #part, "MemphiaS" .. seg[2] } end
      end
    end
  end

  vim.bo[buf].modifiable = true
  if replace_from == 0 and base == 0 and #cur > 0 then
    api.nvim_buf_clear_namespace(buf, NS, n - 1, n)
  end
  api.nvim_buf_set_text(buf, n - 1, replace_from, n - 1, #cur, lines)
  for _, h in ipairs(hls) do
    local row = n - 2 + h[1]
    local off = h[1] == 1 and base or 0
    api.nvim_buf_set_extmark(buf, NS, row, h[2] + off, { end_col = h[3] + off, hl_group = h[4] })
  end
  local total = api.nvim_buf_line_count(buf)
  if total > MAX_LINES then
    api.nvim_buf_set_lines(buf, 0, total - MAX_LINES, false, {})
    total = MAX_LINES
  end
  vim.bo[buf].modifiable = false

  local shown = false
  for _, win in ipairs(fn.win_findbuf(buf)) do
    shown = true
    if S.follow[key] then api.nvim_win_set_cursor(win, { total, 0 }) end
  end
  if not shown and key ~= "agent" and not S.unread[key] then
    S.unread[key] = true
    refresh_bars()
  end
end

local function clear_buf(key)
  local buf = S.bufs[key]
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, {})
  api.nvim_buf_clear_namespace(buf, NS, 0, -1)
  vim.bo[buf].modifiable = false
  S.follow[key] = true
  refresh_bars()
end

-- ---------------------------------------------------------------- queue panel
local function render_queue()
  local st, qb = S.state, S.bufs.queue
  local lines, hls = {}, {}
  local function add(tag, hl, items)
    for i = 1, math.min(3, #items) do
      lines[#lines + 1] = tag .. " " .. oneline(items[i]):sub(1, 200)
      hls[#hls + 1] = { #lines - 1, #tag, hl }
    end
  end
  local steer, tasks = st.steer or {}, st.tasks or {}
  add(" STEER ", "MemphiaQSteer", steer)
  add(" TASK  ", "MemphiaQTask", tasks)
  local hidden = #steer + #tasks - math.min(3, #steer) - math.min(3, #tasks)
  if hidden > 0 then
    lines[#lines + 1] = " … " .. hidden .. " more queued"
    hls[#hls + 1] = { #lines - 1, #lines[#lines], "MemphiaQMore" }
  end

  vim.bo[qb].modifiable = true
  api.nvim_buf_set_lines(qb, 0, -1, false, lines)
  api.nvim_buf_clear_namespace(qb, NS, 0, -1)
  for _, h in ipairs(hls) do api.nvim_buf_set_extmark(qb, NS, h[1], 0, { end_col = h[2], hl_group = h[3] }) end
  vim.bo[qb].modifiable = false

  local w = S.wins
  if #lines == 0 then
    if valid_win(w.queue) then api.nvim_win_close(w.queue, true) end
    w.queue = nil
  elseif valid_win(w.queue) then
    api.nvim_win_set_height(w.queue, #lines)
  elseif valid_win(w.input) then
    w.queue = api.nvim_open_win(qb, false, { split = "above", win = w.input, height = #lines })
    local wo = vim.wo[w.queue]
    wo.winfixheight, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
    wo.wrap, wo.cursorline, wo.statusline = false, false, " "
    wo.winhighlight = "Normal:MemphiaQueue"
  end
end

-- ---------------------------------------------------------------- backend messages
local function on_message(msg)
  local t = msg.t
  if t == "w" then
    append(msg.b, msg.s)
  elseif t == "style" then
    api.nvim_set_hl(0, "MemphiaS" .. msg.id, {
      fg = msg.fg, bg = msg.bg, bold = msg.bold, italic = msg.italic,
      underline = msg.underline, reverse = msg.reverse,
    })
  elseif t == "state" then
    local was_approval = S.state.approval ~= nil
    S.state = msg
    render_queue()
    update_prompt()
    refresh_bars()
    if msg.approval and not was_approval and valid_win(S.wins.input) then
      api.nvim_set_current_win(S.wins.input)
      vim.cmd("startinsert!")
    end
  elseif t == "clear" then
    clear_buf(msg.b)
  elseif t == "focus" then
    M.select_tab(msg.tab)
  elseif t == "input" then
    local ib = S.bufs.input
    local n = api.nvim_buf_line_count(ib)
    api.nvim_buf_set_lines(ib, n - 1, n, false, { S.prompt .. oneline(msg.text) })
    update_prompt()
    if valid_win(S.wins.input) then
      api.nvim_set_current_win(S.wins.input)
      vim.cmd("startinsert!")
    end
  end
end

local function on_stdout(_, data)
  if not S then return end
  data[1] = S.partial .. data[1]
  S.partial = table.remove(data)
  for _, line in ipairs(data) do
    if line ~= "" then
      local ok, msg = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
      if ok and type(msg) == "table" then on_message(msg) end
    end
  end
end

-- Backend stderr (tracebacks, startup errors) goes to the agent pane, highlighted as errors.
local function on_stderr(_, data)
  if not S or (#data == 1 and data[1] == "") then return end
  local buf = S.bufs.agent
  local first = api.nvim_buf_line_count(buf) - 1
  append("agent", { { table.concat(data, "\n"), 0 } })
  local last = api.nvim_buf_line_count(buf) - 1
  for row = first, last do
    if api.nvim_buf_get_lines(buf, row, row + 1, false)[1] ~= "" then
      api.nvim_buf_set_extmark(buf, NS, row, 0, { line_hl_group = "ErrorMsg" })
    end
  end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  append("agent", { { "\n[memphia backend exited with code " .. code .. "]\n", 0 } })
  S.state.busy = false
  refresh_bars()
  if code ~= 0 and vim.v.exiting == vim.NIL then vim.notify("Memphia backend exited with code " .. code, vim.log.levels.WARN) end
end

-- ---------------------------------------------------------------- input line
local function load_history()
  local hist, cur = {}, nil
  local f = io.open(HISTORY_FILE, "r")
  if not f then return hist end
  for line in f:lines() do
    if line:sub(1, 1) == "+" then
      cur = cur and (cur .. "\n" .. line:sub(2)) or line:sub(2)
    elseif cur then
      hist[#hist + 1] = cur
      cur = nil
    end
  end
  if cur then hist[#hist + 1] = cur end
  f:close()
  return hist
end

local function save_history(text)
  if text == "" or S.history[#S.history] == text then return end
  S.history[#S.history + 1] = text
  local f = io.open(HISTORY_FILE, "a")
  if not f then return end
  f:write("\n# " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n")
  for line in (text .. "\n"):gmatch("(.-)\n") do f:write("+" .. line .. "\n") end
  f:close()
end

local function set_input(text)
  local ib = S.bufs.input
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_set_lines(ib, n - 1, n, false, { S.prompt .. text })
  update_prompt()
  if valid_win(S.wins.input) then api.nvim_win_set_cursor(S.wins.input, { n, #S.prompt + #text }) end
end

local function history_step(delta)
  local h = S.history
  if #h == 0 then return end
  if not S.hidx then
    local n = api.nvim_buf_line_count(S.bufs.input)
    S.hdraft = api.nvim_buf_get_lines(S.bufs.input, n - 1, n, false)[1]:sub(#S.prompt + 1)
    S.hidx = #h + 1
  end
  S.hidx = math.max(1, math.min(#h + 1, S.hidx + delta))
  set_input(S.hidx > #h and S.hdraft or oneline(h[S.hidx]))
end

local function on_submit(text)
  S.hidx = nil
  if not S.state.approval then save_history(text) end
  if S.job then
    send({ t = "input", text = text })
  else
    append("agent", { { "backend is not running; :Memphia to restart\n", 0 } })
  end
  -- keep the prompt buffer to a single line
  vim.schedule(function()
    if S and api.nvim_buf_is_valid(S.bufs.input) then
      api.nvim_buf_set_lines(S.bufs.input, 0, -2, false, {})
      update_prompt()
    end
  end)
end

local function fit_input()
  local w = S.wins.input
  if not valid_win(w) then return end
  local n = api.nvim_buf_line_count(S.bufs.input)
  local text = api.nvim_buf_get_lines(S.bufs.input, n - 1, n, false)[1]
  local rows = math.max(1, math.ceil(fn.strdisplaywidth(text) / math.max(1, api.nvim_win_get_width(w))))
  api.nvim_win_set_height(w, math.min(3, rows))
end

-- ---------------------------------------------------------------- layout
local function setup_output_win(win)
  local wo = vim.wo[win]
  wo.wrap, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.foldcolumn, wo.list, wo.spell, wo.cursorline = "0", false, false, false
  wo.statusline = "%#MemphiaHeader#"
end

local function open_agent_win()
  local w = S.wins
  w.agent = api.nvim_open_win(S.bufs.agent, false, { split = "left", win = w.right })
  api.nvim_win_set_width(w.agent, math.floor((api.nvim_win_get_width(w.agent) + api.nvim_win_get_width(w.right)) / 2))
  setup_output_win(w.agent)
  if S.follow.agent then api.nvim_win_set_cursor(w.agent, { api.nvim_buf_line_count(S.bufs.agent), 0 }) end
end

local function build_layout()
  local w = S.wins
  vim.cmd("tabnew")
  S.tab = api.nvim_get_current_tabpage()
  local scratch = api.nvim_get_current_buf()
  w.right = api.nvim_get_current_win()
  api.nvim_win_set_buf(w.right, S.bufs[right_key()])
  if api.nvim_buf_is_valid(scratch) and scratch ~= S.bufs[right_key()] then
    pcall(api.nvim_buf_delete, scratch, { force = true })
  end
  setup_output_win(w.right)
  w.input = api.nvim_open_win(S.bufs.input, true, { split = "below", win = -1, height = 1 })
  local wo = vim.wo[w.input]
  wo.winfixheight, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.wrap, wo.cursorline, wo.foldcolumn = true, false, "0"
  wo.statusline = "%#MemphiaPHint# F1-F6/Tab pane │ C-o scroll target │ PgUp/PgDn S-↑/↓ wheel scroll │ "
    .. "C-x interrupt │ C-c cancel │ C-t agent pane │ ===h help"
  if S.show_agent then open_agent_win() end
  S.wins.queue = nil
  render_queue()
  refresh_bars()
  api.nvim_set_current_win(w.input)
  vim.cmd("startinsert!")
end

local function layout_alive()
  return S.tab and api.nvim_tabpage_is_valid(S.tab) and valid_win(S.wins.right) and valid_win(S.wins.input)
end

-- ---------------------------------------------------------------- actions
function M.select_tab(which)
  if not S then return end
  local i = type(which) == "number" and which or nil
  if not i then
    for idx, key in ipairs(RIGHT_TABS) do if key == which then i = idx end end
  end
  if not i or i < 1 or i > #RIGHT_TABS then return end
  S.right = i
  local key = right_key()
  S.unread[key] = false
  if valid_win(S.wins.right) then
    api.nvim_win_set_buf(S.wins.right, S.bufs[key])
    if S.follow[key] then api.nvim_win_set_cursor(S.wins.right, { api.nvim_buf_line_count(S.bufs[key]), 0 }) end
  end
  refresh_bars()
end

local function cycle_tab(delta) M.select_tab((S.right - 1 + delta) % #RIGHT_TABS + 1) end

local function toggle_agent_pane()
  S.show_agent = not S.show_agent
  if S.show_agent then
    if valid_win(S.wins.right) and not valid_win(S.wins.agent) then open_agent_win() end
  else
    if valid_win(S.wins.agent) then api.nvim_win_close(S.wins.agent, true) end
    S.wins.agent = nil
    S.target = "right"
  end
  refresh_bars()
end

local function follow_target()
  local win = scroll_target_win()
  if not valid_win(win) then return end
  local key = key_of_win(win)
  S.follow[key] = true
  api.nvim_win_set_cursor(win, { api.nvim_buf_line_count(api.nvim_win_get_buf(win)), 0 })
  refresh_bars()
end

local function cancel_or_clear()
  if S.state.busy then
    send({ t = "cancel" })
  else
    set_input("")
  end
end

local function wheel(dir)
  local pos = fn.getmousepos()
  local win = pos.winid
  local key = valid_win(win) and key_of_win(win)
  if key then S.target = key == "agent" and "agent" or "right" end
  if valid_win(win) then scroll_win(win, WHEEL_LINES .. (dir < 0 and "<C-y>" or "<C-e>")) end
  refresh_bars()
end

local function set_keymaps()
  local out = { S.bufs.agent, S.bufs.queue }
  for _, key in ipairs(RIGHT_TABS) do out[#out + 1] = S.bufs[key] end
  local ib = S.bufs.input
  local function map(modes, lhs, rhs, bufs)
    for _, b in ipairs(bufs) do vim.keymap.set(modes, lhs, rhs, { buffer = b, nowait = true, silent = true }) end
  end
  local all = vim.list_extend({ ib }, out)

  for i = 1, #RIGHT_TABS do map({ "n", "i" }, "<F" .. i .. ">", function() M.select_tab(i) end, all) end
  map({ "n", "i" }, "<Tab>", function() cycle_tab(1) end, all)
  map({ "n", "i" }, "<S-Tab>", function() cycle_tab(-1) end, all)
  map({ "n", "i" }, "<C-c>", cancel_or_clear, all)
  map({ "n", "i" }, "<C-x>", function() send({ t = "interrupt" }) end, all)
  map({ "n", "i" }, "<C-t>", toggle_agent_pane, all)
  map({ "n", "i" }, "<C-l>", function() send({ t = "clear", b = right_key() }) end, all)
  map({ "n", "i" }, "<C-q>", M.quit, all)
  map({ "n", "i" }, "<C-o>", function()
    S.target = S.target == "right" and S.show_agent and "agent" or "right"
    refresh_bars()
  end, all)
  map({ "n", "i" }, "<ScrollWheelUp>", function() wheel(-1) end, all)
  map({ "n", "i" }, "<ScrollWheelDown>", function() wheel(1) end, all)

  -- From the input line, scrolling keys act on the scroll target (Ctrl-O toggles it).
  map("i", "<PageUp>", function() scroll_win(scroll_target_win(), "<C-b>") end, { ib })
  map("i", "<PageDown>", function() scroll_win(scroll_target_win(), "<C-f>") end, { ib })
  map("i", "<S-Up>", function() scroll_win(scroll_target_win(), "<C-y>") end, { ib })
  map("i", "<S-Down>", function() scroll_win(scroll_target_win(), "<C-e>") end, { ib })
  map({ "n", "i" }, "<F12>", follow_target, { ib })
  map({ "n", "i" }, "<C-End>", follow_target, { ib })
  map("i", "<Up>", function() history_step(-1) end, { ib })
  map("i", "<Down>", function() history_step(1) end, { ib })

  -- In an output pane: F12/G follow, i/a/<CR> jump to the input line.
  local function follow_here()
    local win = api.nvim_get_current_win()
    local key = key_of_win(win)
    if key then S.follow[key] = true end
    api.nvim_win_set_cursor(win, { api.nvim_buf_line_count(0), 0 })
    refresh_bars()
  end
  map("n", "<F12>", follow_here, out)
  map("n", "G", follow_here, out)
  local function to_input()
    if valid_win(S.wins.input) then
      api.nvim_set_current_win(S.wins.input)
      vim.cmd("startinsert!")
    end
  end
  for _, lhs in ipairs({ "i", "a", "I", "A", "o", "<CR>" }) do map("n", lhs, to_input, out) end
end

local function set_autocmds()
  local group = api.nvim_create_augroup("Memphia", { clear = true })
  api.nvim_create_autocmd("WinScrolled", {
    group = group,
    callback = function()
      if not S then return end
      for _, win in ipairs({ S.wins.agent, S.wins.right }) do
        if valid_win(win) then
          local key = key_of_win(win)
          local f = last_visible(win)
          if S.follow[key] ~= f then
            S.follow[key] = f
            refresh_bars()
          end
        end
      end
    end,
  })
  api.nvim_create_autocmd({ "TextChangedI", "TextChanged" }, {
    group = group, buffer = S.bufs.input, callback = fit_input,
  })
  api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function() if S and S.job then send({ t = "quit" }) end end,
  })
end

local function create_buffers()
  local bufs = {}
  for key, title in pairs(TITLES) do
    local b = api.nvim_create_buf(false, true)
    pcall(api.nvim_buf_set_name, b, "memphia://" .. title:lower())
    vim.bo[b].bufhidden, vim.bo[b].swapfile, vim.bo[b].modifiable = "hide", false, false
    vim.bo[b].filetype = "memphia"
    bufs[key] = b
  end
  bufs.queue = api.nvim_create_buf(false, true)
  vim.bo[bufs.queue].bufhidden, vim.bo[bufs.queue].modifiable = "hide", false
  local ib = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, ib, "memphia://input")
  vim.bo[ib].buftype, vim.bo[ib].bufhidden, vim.bo[ib].swapfile = "prompt", "hide", false
  fn.prompt_setcallback(ib, on_submit)
  bufs.input = ib
  return bufs
end

local function normalize_args(args)
  if args == nil then args = vim.g.memphia_args end
  if type(args) == "string" then args = vim.split(vim.trim(args), "%s+", { trimempty = true }) end
  return args or {}
end

-- ---------------------------------------------------------------- public API
function M.start(args)
  define_highlights()
  if S and S.job then
    if not layout_alive() then build_layout() else api.nvim_set_current_tabpage(S.tab) end
    return
  end
  args = normalize_args(args)
  local fresh = S == nil
  if fresh then
    S = {
      bufs = create_buffers(), wins = {}, right = 1, target = "right", show_agent = true,
      follow = {}, unread = {}, state = { phase = "starting" }, prompt = "", partial = "",
      history = load_history(),
    }
    for key in pairs(TITLES) do S.follow[key] = true end
    set_keymaps()
    set_autocmds()
  end
  S.partial = ""
  local cmd = { vim.g.memphia_python or "python3", "-u", SCRIPT_DIR .. "/memphia_nvim.py" }
  vim.list_extend(cmd, args)
  S.job = fn.jobstart(cmd, {
    cwd = fn.getcwd(), on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit,
  })
  if S.job <= 0 then
    S.job = nil
    vim.notify("Memphia: failed to start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
    return
  end
  if not layout_alive() then build_layout() else api.nvim_set_current_tabpage(S.tab) end
  update_prompt()
  if not S.timer then
    S.timer = uv.new_timer()
    S.timer:start(250, 250, vim.schedule_wrap(function()
      if S and S.state.busy and valid_win(S.wins.input) then vim.wo[S.wins.input].winbar = status_bar() end
    end))
  end
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 2000)
  end
  if S.timer then
    S.timer:stop()
    S.timer:close()
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) and #api.nvim_list_tabpages() > 1 then
    pcall(vim.cmd, "tabclose " .. api.nvim_tabpage_get_number(S.tab))
  end
  for _, b in pairs(S.bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  pcall(api.nvim_del_augroup_by_name, "Memphia")
  S = nil
end

api.nvim_create_user_command("Memphia", function(o) M.start(#o.fargs > 0 and o.fargs or nil) end,
  { nargs = "*", desc = "Open the Memphia agent UI" })
api.nvim_create_user_command("MemphiaQuit", M.quit, { desc = "Stop Memphia and close its UI" })

package.loaded.memphia = M

-- `:source memphia.lua` opens the UI right away; `require("memphia")` from a config does not.
if MODNAME == nil and not vim.g.memphia_no_autostart then M.start() end

return M
