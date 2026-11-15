-- mksk.lua: Neovim frontend for mksk, the make-sketch planning agent (requires Neovim >= 0.10).
--
-- Cloned from neos.lua: ONE floating window over ONE persistent chat buffer, the same prompt line, layouts, peek,
-- raw LLM log (F2), steering and input history. Trimmed to planning, with an IFACE pane (F8) for the network.
--
-- The agent runs in mksk.py (next to this file). It plans with you and keeps a plan file in the make-sketch format
-- (mksk--<plan_name>--YYYY-MM-DD.md, ~/echo-home/Documents/specs/make-sketch.md). At start it opens the most
-- up-to-date plan file in the current directory, else a zero plan. Every read and edit of the plan goes through
-- the built-in functions of mksk_plan.lua (weights, table, targets, notes, diff, task brief), run here on the agent's
-- request. Edits are staged: nothing is written until you approve the diff, and then only to today's file. The chat
-- stays in the agent's context from start to quit. Task assignment exists only while mksk is on the network.
--
--   :source /path/to/mksk.lua        start + open (backend args from vim.g.mksk_args)
--   :Mksk [plan] [--port 8080 ...]   open/focus the float, starting the backend (a plan name overrides the newest plan;
--                                    a new name starts a new plan)
--   :MkskToggle                      show / focus / hide the float (default key <M-m>, also in visual mode:
--                                    attaches the selection to your next message)
--   :[range]MkskAsk [text]           send text (with the range as [SELECTION]) without typing in the float
--   :MkskDiff                        saved | proposed plan, side by side in a tab (q closes it)
--   :MkskFile                        open the plan file in a tab
--   :MkskLayout center|left|right|top|bottom|full   (no argument: cycle)
--   :MkskQuit                        stop the backend and wipe the chat
--   :InetJoin mksk / :InetLeave mksk join / leave the Intelligence Network (profile g:inet_agents.mksk, optional)
--
-- Options: vim.g.mksk_python (default "python3", "python" on Windows), vim.g.mksk_args (list or string),
--          vim.g.mksk_toggle_key (default "<M-m>", "" = no global map), vim.g.mksk_layout ("right"),
--          vim.g.mksk_dir (plan folder; default: the current directory when mksk starts), vim.g.mksk_plan.
--
-- In the float: the last line is the input (a prompt buffer). Everything above is the transcript —
-- normal-mode motions, search and yank work as usual.
--   <CR> send │ ↑/↓ history │ C-x interrupt LLM │ C-c cancel request / clear line │ F2 raw LLM log │ F8 network
--   M-h/M-j/M-k/M-l dock left/bottom/top/right │ M-c center │ M-f full │ M-b peek │ q / <Esc> (normal) hide
--   <CR> (normal) on a numbered option picks it
-- By hand: :lua require("mksk").plan.<function>{...}   (see mksk_plan.lua)

local MODNAME = ...
do -- sourcing again while mksk runs would orphan the running backend: show it instead
  local prev = package.loaded.mksk
  if type(prev) == "table" and prev.running and prev.running() then
    if MODNAME == nil then prev.show(true) end
    return prev
  end
end

local M = {}
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

--- Git Bash/MSYS hands nvim paths like /c/Users/..; native Windows python needs C:/Users/.. (as in knbg.lua).
local function native(path)
  if fn.has("win32") == 1 and path then
    local drive, rest = path:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(path) == 0 and fn.filereadable(path) == 0 then path = drive:upper() .. ":/" .. rest end
    path = path:gsub("\\", "/")
  end
  return path
end

local SCRIPT_DIR = native(fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h"))
local P = dofile(SCRIPT_DIR .. "/mksk_plan.lua")
M.plan = P

local HISTORY_FILE = fn.stdpath("data") .. "/mksk_history"
local LAYOUTS = { "center", "left", "right", "top", "bottom", "full" }
local MAX_LINES = 10000
local NS = api.nvim_create_namespace("mksk")
local NS_PROMPT = api.nvim_create_namespace("mksk_prompt")
local SPINNER = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }
local VIEW_TITLE = { chat = " MKSK ", log = " MKSK · LOG ", iface = " MKSK · IFACE " }

local S -- session state; nil when nothing is running

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    MkskBorder = { fg = "#08a4c4" },
    MkskBadge = { bg = "#08a4c4", fg = "#000000", bold = true },
    MkskTitle = { bg = "#303030", fg = "#d0d0d0" },
    MkskIdle = { bg = "#005f00", fg = "#ffffff", bold = true },
    MkskBusy = { bg = "#af5f00", fg = "#ffffff", bold = true },
    MkskHint = { fg = "#808080" },
    MkskSteer = { bg = "#5f00af", fg = "#ffffff", bold = true },
    MkskApprove = { bg = "#af0000", fg = "#ffffff", bold = true },
    MkskUser = { fg = "#5fd7ff", bold = true },
    MkskAttach = { bg = "#005f87", fg = "#ffffff" },
  }
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
end

-- ---------------------------------------------------------------- helpers
local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function is_float(w) return api.nvim_win_get_config(w).relative ~= "" end
local function oneline(s) return (tostring(s or ""):gsub("[\r\n]+", " ")) end
local function clean_dir(d) return (native(fn.fnamemodify(fn.expand(d), ":p")):gsub("[/\\]$", "")) end

local function send(msg)
  if S and S.job then pcall(fn.chansend, S.job, vim.json.encode(msg) .. "\n") end
end

local function float_shown()
  return S and valid_win(S.win) and api.nvim_win_get_tabpage(S.win) == api.nvim_get_current_tabpage()
end

local function target_win()
  if valid_win(S.target) and not is_float(S.target) then return S.target end
  for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
    if not is_float(w) then
      S.target = w
      return w
    end
  end
end

local function remember_target()
  local w = api.nvim_get_current_win()
  if w ~= S.win and not is_float(w) then S.target = w end
end

--- Where the user is (the last editor window, floats ignored): lets "this target" refer to the plan file there.
local function position(win)
  win = win or target_win()
  if not win then return nil end
  local buf = api.nvim_win_get_buf(win)
  local cur = api.nvim_win_get_cursor(win)
  return { file = native(api.nvim_buf_get_name(buf)), filetype = vim.bo[buf].filetype, line = cur[1], col = cur[2] + 1,
    text = (api.nvim_buf_get_lines(buf, cur[1] - 1, cur[1], false)[1] or ""):sub(1, 300) }
end

-- ---------------------------------------------------------------- float title / footer
local function title_chunks()
  local st = S.state
  local spin = st.busy and SPINNER[math.floor(uv.hrtime() / 1e8) % #SPINNER + 1] or "●"
  local t = {
    { VIEW_TITLE[S.view] or " MKSK ", "MkskBadge" },
    { " " .. spin .. " " .. oneline(st.phase or "starting") .. " ", st.busy and "MkskBusy" or "MkskIdle" },
    { " " .. oneline(st.model or "-") .. " ", "MkskTitle" },
  }
  if S.peek then t[#t + 1] = { " peek ", "MkskTitle" } end
  if not S.job then t[#t + 1] = { " backend stopped ", "MkskApprove" } end
  return t
end

local function footer_chunks()
  local st, f = S.state, {}
  local q = {}
  if st.steer and #st.steer > 0 then q[#q + 1] = #st.steer .. " steer" end
  if st.tasks and #st.tasks > 0 then q[#q + 1] = #st.tasks .. " task" end
  if #q > 0 then f[#f + 1] = { " ⏳ " .. table.concat(q, " · ") .. " ", "MkskSteer" } end
  if (st.deferred or 0) > 0 then f[#f + 1] = { " ⏸ " .. st.deferred .. " to review: ===p ", "MkskApprove" } end
  if S.sel then f[#f + 1] = { (" 📎 L%d-%d "):format(S.sel.start, S.sel["end"]), "MkskAttach" } end
  if st.plan then f[#f + 1] = { " ▤ " .. oneline(st.plan) .. (st.staged and " · staged" or "") .. " ", "MkskTitle" } end
  local net = st.net or {}
  if net.joined then
    f[#f + 1] = { " 🌐 @" .. oneline(net.name) .. " " .. (net.connected and oneline(net.state) or "connecting") .. " ", "MkskTitle" }
  else
    f[#f + 1] = { " 🌐 off ", "MkskTitle" }
  end
  f[#f + 1] = { " F2 log │ F8 net │ M-h/j/k/l dock │ M-b peek │ q hide ", "MkskHint" }
  return f
end

local function float_config()
  local cols = vim.o.columns
  local rows = vim.o.lines - vim.o.cmdheight - (vim.o.laststatus > 0 and 1 or 0)
  local layout = LAYOUTS[S.layout]
  local w, h, r, c
  if layout == "right" then
    w = math.max(50, math.floor(cols * 0.42)); h = rows - 2; r = 0; c = cols - w - 2
  elseif layout == "left" then
    w = math.max(50, math.floor(cols * 0.42)); h = rows - 2; r = 0; c = 0
  elseif layout == "top" then
    w = cols - 2; h = math.max(8, math.floor(rows * 0.4)); r = 0; c = 0
  elseif layout == "bottom" then
    w = cols - 2; h = math.max(8, math.floor(rows * 0.4)); r = rows - h - 2; c = 0
  elseif layout == "full" then
    w = cols - 2; h = rows - 2; r = 0; c = 0
  else
    w = math.floor(cols * 0.72); h = math.floor(rows * 0.72)
    r = math.floor((rows - h) / 2) - 1; c = math.floor((cols - w) / 2)
  end
  w, h = math.max(20, math.min(w, cols - 2)), math.max(5, math.min(h, rows - 2))
  return {
    relative = "editor", width = w, height = h, row = math.max(0, r), col = math.max(0, c),
    border = "rounded", title = title_chunks(), title_pos = "left",
    footer = footer_chunks(), footer_pos = "right", zindex = 60,
  }
end

local function refresh_float()
  if not float_shown() then return end
  pcall(api.nvim_win_set_config, S.win, float_config())
end

-- ---------------------------------------------------------------- prompt line
local function prompt_spec()
  local st = S.state
  if st.approval then return { { " " .. oneline(st.approval.prompt or "ANSWER") .. " ", "MkskApprove" }, { " ", nil } } end
  if st.busy and st.net_busy then return { { " QUEUE ", "MkskSteer" }, { " next request ", "MkskHint" }, { " ", nil } } end
  if st.busy then return { { " STEER ", "MkskSteer" }, { " !now +task ", "MkskHint" }, { " ", nil } } end
  return { { " MKSK ", "MkskBadge" }, { " ❯ ", "MkskHint" } }
end

local function update_prompt()
  local spec = prompt_spec()
  local text = ""
  for _, p in ipairs(spec) do text = text .. p[1] end
  local ib, old = S.bufs.chat, S.prompt
  if text ~= old then
    fn.prompt_setprompt(ib, text)
    local n = api.nvim_buf_line_count(ib)
    local line = api.nvim_buf_get_lines(ib, n - 1, n, false)[1]
    if old ~= "" and line:sub(1, #old) == old then
      api.nvim_buf_set_text(ib, n - 1, 0, n - 1, #old, { text })
    elseif line:sub(1, #text) ~= text then
      api.nvim_buf_set_lines(ib, n - 1, n, false, { text .. line })
    end
    S.prompt = text
  end
  local n, col = api.nvim_buf_line_count(ib), 0
  api.nvim_buf_clear_namespace(ib, NS_PROMPT, 0, -1)
  for _, p in ipairs(spec) do
    if p[2] then api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, col, { end_col = col + #p[1], hl_group = p[2] }) end
    col = col + #p[1]
  end
end

local function input_text()
  local ib = S.bufs.chat
  local n = api.nvim_buf_line_count(ib)
  return api.nvim_buf_get_lines(ib, n - 1, n, false)[1]:sub(#S.prompt + 1)
end

local function set_input(text)
  local ib = S.bufs.chat
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_set_lines(ib, n - 1, n, false, { S.prompt .. text })
  update_prompt()
  if float_shown() and api.nvim_win_get_buf(S.win) == ib then
    api.nvim_win_set_cursor(S.win, { n, #S.prompt + #text })
  end
end

-- ---------------------------------------------------------------- appending output
-- chat      : a prompt buffer — output goes above the prompt (last) line.
-- log, iface: plain read-only buffers — output goes at the end.
-- S.tail[key] is true when the last output line is unfinished (no trailing newline yet).
local function append(key, segs)
  local buf = S.bufs[key]
  if not buf or not api.nvim_buf_is_valid(buf) then return end
  local lines, hls = { "" }, {}
  for _, seg in ipairs(segs) do
    local parts = vim.split(seg[1], "\n", { plain = true })
    for i, part in ipairs(parts) do
      if i > 1 then lines[#lines + 1] = "" end
      if #part > 0 then
        local s = #lines[#lines]
        lines[#lines] = lines[#lines] .. part
        if seg[2] ~= 0 then hls[#hls + 1] = { #lines, s, s + #part, "MkskS" .. seg[2] } end
      end
    end
  end
  if #lines == 1 and lines[1] == "" then return end
  local closed = lines[#lines] == ""
  if closed then table.remove(lines) end

  local is_log = key ~= "chat"
  if is_log then vim.bo[buf].modifiable = true end
  local count = api.nvim_buf_line_count(buf)
  local endrow = is_log and count or count - 1 -- rows [0, endrow) are output
  local start_row, first_col
  if S.tail[key] and endrow > 0 then
    local row = endrow - 1
    local cur = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    start_row, first_col = row, #cur
    api.nvim_buf_set_text(buf, row, #cur, row, #cur, lines)
  else
    start_row, first_col = endrow, 0
    if is_log and count == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == "" then
      api.nvim_buf_set_lines(buf, 0, 1, false, lines) -- fresh buffer: reuse its empty line
    else
      api.nvim_buf_set_lines(buf, endrow, endrow, false, lines)
    end
  end
  for _, h in ipairs(hls) do
    local off = h[1] == 1 and first_col or 0
    pcall(api.nvim_buf_set_extmark, buf, NS, start_row + h[1] - 1, h[2] + off, { end_col = h[3] + off, hl_group = h[4] })
  end
  S.tail[key] = not closed

  local total = api.nvim_buf_line_count(buf)
  if total > MAX_LINES then api.nvim_buf_set_lines(buf, 0, total - MAX_LINES, false, {}) end
  if is_log then vim.bo[buf].modifiable = false end

  -- follow: keep the window at the bottom when it was already there
  for _, win in ipairs(fn.win_findbuf(buf)) do
    local last = api.nvim_buf_line_count(buf)
    local row = api.nvim_win_get_cursor(win)[1]
    if is_log and row >= last - #lines - 1 then
      api.nvim_win_set_cursor(win, { last, 0 })
    elseif not is_log and row == last then
      api.nvim_win_call(win, function() vim.cmd("normal! zb") end)
    end
  end
end

local function clear_buf(key)
  local buf = S.bufs[key]
  if key ~= "chat" then
    vim.bo[buf].modifiable = true
    api.nvim_buf_set_lines(buf, 0, -1, false, {})
    vim.bo[buf].modifiable = false
  else
    local n = api.nvim_buf_line_count(buf)
    api.nvim_buf_set_lines(buf, 0, n - 1, false, {})
  end
  api.nvim_buf_clear_namespace(buf, NS, 0, -1)
  S.tail[key] = key ~= "chat"
end

-- ---------------------------------------------------------------- requests from mksk.py
local OPS = {}

--- Every plan read and edit of the agent is one of the built-in functions of mksk_plan.lua.
function OPS.plan(p)
  local f = P[p.fn]
  if type(f) ~= "function" or tostring(p.fn):sub(1, 1) == "_" then error("unknown plan function " .. tostring(p.fn), 0) end
  return f(p.args or {})
end

function OPS.context() return position(target_win()) end

local function open_file()
  local ok, st = pcall(P.state)
  if not ok or not st.open then error("no plan is open", 0) end
  if (st.base_file or "") == "" then error("the plan has no file yet (nothing was approved so far)", 0) end
  if api.nvim_get_current_win() == (S and S.win) then vim.cmd("stopinsert") end
  local w = S and target_win()
  if valid_win(w) then api.nvim_set_current_win(w) end
  vim.cmd("tabnew " .. fn.fnameescape(st.base_file))
  if S then S.target = api.nvim_get_current_win() end
  return "opened " .. st.base_file .. " in a new tab"
end

function OPS.open_file() return open_file() end

local function handle_req(msg)
  local res = { t = "res", id = msg.id }
  local op = OPS[msg.op]
  if not op then
    res.ok, res.err = false, "unknown op " .. tostring(msg.op)
    return send(res)
  end
  local ok, out = pcall(op, msg)
  res.ok = ok
  if ok then res.out = out else res.err = tostring(out) end
  send(res)
  if float_shown() then refresh_float() end
end

-- ---------------------------------------------------------------- float window
local function setup_win(win)
  local wo = vim.wo[win]
  wo.wrap, wo.linebreak, wo.number, wo.relativenumber = true, true, false, false
  wo.signcolumn, wo.foldcolumn, wo.cursorline, wo.list, wo.spell = "no", "0", false, false, false
  wo.winhighlight = "Normal:NormalFloat,FloatBorder:MkskBorder"
  pcall(function() wo.winfixbuf = true end)
end

local function put_view(view)
  S.view = view
  pcall(function() vim.wo[S.win].winfixbuf = false end)
  api.nvim_win_set_buf(S.win, S.bufs[view])
  pcall(function() vim.wo[S.win].winfixbuf = true end)
end

local function enter_input()
  if not float_shown() then return end
  if S.view ~= "chat" then
    put_view("chat")
    refresh_float()
  end
  api.nvim_set_current_win(S.win)
  local n = api.nvim_buf_line_count(S.bufs.chat)
  api.nvim_win_set_cursor(S.win, { n, #api.nvim_buf_get_lines(S.bufs.chat, n - 1, n, false)[1] })
  vim.cmd("startinsert!")
end

function M.show(enter)
  if not S then return M.start() end
  if enter ~= false then remember_target() end
  if valid_win(S.win) and not float_shown() then pcall(api.nvim_win_close, S.win, true) end
  if not float_shown() then
    S.win = api.nvim_open_win(S.bufs[S.view], false, float_config())
    setup_win(S.win)
    if S.peek then vim.wo[S.win].winblend = vim.g.mksk_peek_blend or 70 end
    local n = api.nvim_buf_line_count(S.bufs[S.view])
    api.nvim_win_set_cursor(S.win, { n, 0 })
  end
  if enter ~= false then
    if S.view == "chat" then enter_input() else api.nvim_set_current_win(S.win) end
  end
end

function M.hide()
  if S and valid_win(S.win) then
    if api.nvim_get_current_win() == S.win then vim.cmd("stopinsert") end
    pcall(api.nvim_win_close, S.win, true)
  end
  if S then S.win = nil end
end

function M.toggle()
  if not S then return M.start() end
  if float_shown() and api.nvim_get_current_win() == S.win then return M.hide() end
  M.show(true)
end

-- peek: make the float translucent to read the windows behind it (needs 'termguicolors'), toggle back
function M.peek()
  if not float_shown() then return end
  S.peek = not S.peek
  vim.wo[S.win].winblend = S.peek and (vim.g.mksk_peek_blend or 70) or 0
  refresh_float()
end

function M.set_layout(name)
  if not S then return end
  if name and name ~= "" then
    for i, l in ipairs(LAYOUTS) do if l == name then S.layout = i end end
  else
    S.layout = S.layout % #LAYOUTS + 1
  end
  refresh_float()
end

--- F2 chat ⇄ raw LLM log, F8 chat ⇄ IFACE (network traffic and decisions).
local function toggle_view(view)
  if not float_shown() then return end
  put_view(S.view == view and "chat" or view)
  local n = api.nvim_buf_line_count(S.bufs[S.view])
  api.nvim_win_set_cursor(S.win, { n, 0 })
  if S.view == "chat" then enter_input() else vim.cmd("stopinsert") end
  refresh_float()
end

-- ---------------------------------------------------------------- backend messages
local function on_message(msg)
  local t = msg.t
  if t == "w" then
    append(msg.b, msg.s)
  elseif t == "style" then
    api.nvim_set_hl(0, "MkskS" .. msg.id, {
      fg = msg.fg, bg = msg.bg, bold = msg.bold, italic = msg.italic, underline = msg.underline, reverse = msg.reverse,
    })
  elseif t == "state" then
    S.state = msg
    update_prompt()
    refresh_float()
  elseif t == "req" then
    vim.schedule(function() if S then handle_req(msg) end end)
  elseif t == "clear" then
    clear_buf(msg.b)
  elseif t == "show" then
    M.show(true)
  elseif t == "input" then
    set_input(oneline(msg.text))
    if float_shown() then enter_input() end
  elseif t == "notify" then
    if not float_shown() then vim.notify("mksk: " .. msg.text, msg.level == "warn" and vim.log.levels.WARN or vim.log.levels.INFO) end
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

local function on_stderr(_, data)
  if not S or (#data == 1 and data[1] == "") then return end
  local text = table.concat(data, "\n")
  if text:match("%S") then append("log", { { text .. "\n", 0 } }) end
  if text:match("Traceback") or text:match("Error") then
    append("chat", { { "backend error (F2 for the log): " .. oneline(text):sub(1, 200) .. "\n", 0 } })
  end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  S.state.busy, S.state.approval = false, nil
  pcall(P.discard) -- edits the backend staged are orphaned now
  append("chat", { { "[mksk backend exited with code " .. code .. " — :Mksk to restart]\n", 0 } })
  update_prompt()
  refresh_float()
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

local function history_step(delta)
  local h = S.history
  if #h == 0 then return end
  if not S.hidx then
    S.hdraft = input_text()
    S.hidx = #h + 1
  end
  S.hidx = math.max(1, math.min(#h + 1, S.hidx + delta))
  set_input(S.hidx > #h and S.hdraft or oneline(h[S.hidx]))
end

-- Mark a submitted (or MkskAsk) line in the transcript: badge part + user text.
local function mark_user_line(row, plen)
  local ib = S.bufs.chat
  local line = api.nvim_buf_get_lines(ib, row, row + 1, false)[1] or ""
  pcall(api.nvim_buf_set_extmark, ib, NS, row, 0, { end_col = math.min(plen, #line), hl_group = "MkskHint" })
  pcall(api.nvim_buf_set_extmark, ib, NS, row, math.min(plen, #line), { end_col = #line, hl_group = "MkskUser" })
end

local function submit(text, sel)
  S.hidx = nil
  if not S.state.approval then save_history(text) end
  if S.job then
    send({ t = "input", text = text, sel = sel, ctx = position(target_win()) })
  else
    append("chat", { { "backend is not running; :Mksk to restart\n", 0 } })
  end
end

local function on_submit(text)
  -- the prompt buffer already turned the input into a transcript line (row n-2) and added a new prompt
  local n = api.nvim_buf_line_count(S.bufs.chat)
  mark_user_line(n - 2, #S.prompt)
  S.tail.chat = false
  local sel = S.sel
  if vim.trim(text) ~= "" then S.sel = nil end
  submit(text, sel)
  vim.schedule(function() if S then update_prompt() refresh_float() end end)
end

-- ---------------------------------------------------------------- selection capture
local function capture_selection(buf, l1, l2)
  if l1 > l2 then l1, l2 = l2, l1 end
  local rows = {}
  for i, l in ipairs(api.nvim_buf_get_lines(buf, l1 - 1, l2, false)) do rows[#rows + 1] = (l1 + i - 1) .. "| " .. l end
  return { file = native(api.nvim_buf_get_name(buf)), buf = buf, start = l1, ["end"] = l2, text = table.concat(rows, "\n") }
end

function M.toggle_visual()
  -- read the selection before anything moves the cursor or focus
  local l1, l2 = fn.line("v"), fn.line(".")
  local buf, win = api.nvim_get_current_buf(), api.nvim_get_current_win()
  vim.cmd('execute "normal! \\<Esc>"')
  if not S or not S.job then M.start() end
  if not S then return end
  if buf ~= S.bufs.chat and buf ~= S.bufs.log and buf ~= S.bufs.iface then
    S.sel = capture_selection(buf, l1, l2)
    if not is_float(win) then S.target = win end
  end
  if not float_shown() or api.nvim_get_current_win() ~= S.win then M.show(true) end
  refresh_float()
end

function M.ask(text, o)
  if not S or not S.job then M.start() end
  if not S or not S.job then return end
  remember_target()
  local sel = S.sel
  if o and o.range and o.range > 0 then
    sel = capture_selection(api.nvim_get_current_buf(), o.line1, o.line2)
  end
  if not text or vim.trim(text) == "" then
    S.sel = sel
    return M.show(true)
  end
  S.sel = nil
  append("chat", { { S.prompt .. text .. "\n", 0 } })
  local n = api.nvim_buf_line_count(S.bufs.chat)
  mark_user_line(n - 2, #S.prompt)
  submit(text, sel)
  if not float_shown() then M.show(false) end
end

-- ---------------------------------------------------------------- keymaps / autocmds
local function set_keymaps()
  local cb, lb, ib = S.bufs.chat, S.bufs.log, S.bufs.iface
  local function map(modes, lhs, rhs, bufs)
    for _, b in ipairs(bufs) do vim.keymap.set(modes, lhs, rhs, { buffer = b, nowait = true, silent = true }) end
  end
  local all = { cb, lb, ib }
  map({ "n", "i" }, "<C-x>", function() send({ t = "interrupt" }) end, all)
  map({ "n", "i" }, "<C-c>", function()
    if S.state.busy then send({ t = "cancel" }) else set_input("") end
  end, all)
  map({ "n", "i" }, "<F2>", function() toggle_view("log") end, all)
  map({ "n", "i" }, "<F8>", function() toggle_view("iface") end, all)
  -- dock the float to a side so the windows behind it stay visible; M-b makes it translucent (peek)
  map({ "n", "i" }, "<M-h>", function() M.set_layout("left") end, all)
  map({ "n", "i" }, "<M-l>", function() M.set_layout("right") end, all)
  map({ "n", "i" }, "<M-k>", function() M.set_layout("top") end, all)
  map({ "n", "i" }, "<M-j>", function() M.set_layout("bottom") end, all)
  map({ "n", "i" }, "<M-c>", function() M.set_layout("center") end, all)
  map({ "n", "i" }, "<M-f>", function() M.set_layout("full") end, all)
  map({ "n", "i" }, "<M-b>", M.peek, all)
  map("n", "q", M.hide, all)
  map("n", "<Esc>", M.hide, all)
  map("i", "<Up>", function() history_step(-1) end, { cb })
  map("i", "<Down>", function() history_step(1) end, { cb })
  for _, lhs in ipairs({ "i", "a", "I", "A", "o" }) do map("n", lhs, enter_input, all) end
  -- <CR> on "  2. option" while mksk waits for a pick answers with that number; elsewhere it goes to the input line
  map("n", "<CR>", function()
    local row = api.nvim_win_get_cursor(0)[1]
    local text = api.nvim_buf_get_lines(0, row - 1, row, false)[1] or ""
    local num = text:match("^%s+(%d+)%.%s")
    local ap = S.state.approval
    if num and ap and ap.kind == "choice" and S.job then
      M.ask(num)
      enter_input()
    else
      enter_input()
    end
  end, all)
end

local function set_autocmds()
  local group = api.nvim_create_augroup("Mksk", { clear = true })
  api.nvim_create_autocmd("WinClosed", {
    group = group,
    callback = function(ev)
      if S and tonumber(ev.match) == S.win then S.win = nil end
    end,
  })
  api.nvim_create_autocmd("VimResized", { group = group, callback = function() if S then refresh_float() end end })
  -- follow the user's position: the window "this target" refers to (the float itself is ignored)
  api.nvim_create_autocmd({ "WinEnter", "BufEnter", "TabEnter" }, {
    group = group,
    callback = function()
      if not S then return end
      remember_target()
      if float_shown() then refresh_float() end
    end,
  })
  api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function() if S and S.job then send({ t = "quit" }) end end,
  })
end

local function create_buffers()
  local cb = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, cb, "mksk://chat")
  vim.bo[cb].buftype, vim.bo[cb].bufhidden, vim.bo[cb].swapfile = "prompt", "hide", false
  vim.bo[cb].filetype = "mksk"
  fn.prompt_setcallback(cb, on_submit)
  fn.prompt_setinterrupt(cb, function() send({ t = "interrupt" }) end)
  local bufs = { chat = cb }
  for key, ft in pairs({ log = "mksklog", iface = "mkskiface" }) do
    local b = api.nvim_create_buf(false, true)
    pcall(api.nvim_buf_set_name, b, "mksk://" .. key)
    vim.bo[b].bufhidden, vim.bo[b].swapfile, vim.bo[b].modifiable = "hide", false, false
    vim.bo[b].filetype = ft
    bufs[key] = b
  end
  return bufs
end

local function normalize_args(args)
  if args == nil then args = vim.g.mksk_args end
  if type(args) == "string" then args = vim.split(vim.trim(args), "%s+", { trimempty = true }) end
  local out = {}
  for _, a in ipairs(args or {}) do out[#out + 1] = tostring(a) end
  return out
end

-- ---------------------------------------------------------------- public API
--- M.start([plan, ] args...). The plan folder is g:mksk_dir, else the current directory. Without a plan name
--- (argument or g:mksk_plan) mksk opens the most up-to-date mksk--<plan>--YYYY-MM-DD.md there, else a zero plan.
function M.start(args)
  define_highlights()
  if S and S.job then return M.show(true) end
  args = normalize_args(args)
  local dir, plan = clean_dir(vim.g.mksk_dir or fn.getcwd()), vim.g.mksk_plan
  if args[1] and args[1]:sub(1, 1) ~= "-" then plan = table.remove(args, 1) end
  if S == nil then
    local layout = 3
    for i, l in ipairs(LAYOUTS) do if l == vim.g.mksk_layout then layout = i end end
    S = {
      bufs = create_buffers(), view = "chat", layout = layout, tail = { chat = false, log = true, iface = true },
      state = { phase = "starting" }, prompt = "", partial = "", history = load_history(),
    }
    remember_target()
    set_keymaps()
    set_autocmds()
  end
  S.partial = ""
  pcall(P.discard)
  P.setup({ dir = dir })
  -- On Windows "python3" is often the Microsoft Store stub; prefer "python" there (as memphia does).
  local py = vim.g.mksk_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", SCRIPT_DIR .. "/mksk.py", "--dir", dir }
  if plan then vim.list_extend(cmd, { "--plan", tostring(plan) }) end
  vim.list_extend(cmd, args)
  -- tell the backend where the inet driver lives, as a clean native path (Git Bash: /c/.. -> C:/..)
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = dir, env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    vim.notify("mksk: failed to start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
    return
  end
  update_prompt()
  if not S.timer then
    S.timer = uv.new_timer()
    S.timer:start(150, 150, vim.schedule_wrap(function()
      if S and S.state.busy and float_shown() then refresh_float() end
    end))
  end
  M.show(true)
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  if S.timer then
    S.timer:stop()
    S.timer:close()
  end
  M.hide()
  for _, b in pairs(S.bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  pcall(api.nvim_del_augroup_by_name, "Mksk")
  pcall(P.discard)
  S = nil
end

function M.running() return S ~= nil and S.job ~= nil end
function M.status() return S and S.job and S.state.net or nil end
function M.state() return S and S.state or nil end

api.nvim_create_user_command("Mksk", function(o)
  if S and S.job then return M.show(true) end
  local plan = o.fargs[1] and o.fargs[1]:sub(1, 1) ~= "-" and table.remove(o.fargs, 1) or nil
  local args = vim.list_extend(normalize_args(nil), o.fargs) -- typed args come last, so they win
  if plan then table.insert(args, 1, plan) end
  M.start(args)
end, { nargs = "*", desc = "Open the mksk float (starts the backend): [plan] [backend args]" })
api.nvim_create_user_command("MkskToggle", function() M.toggle() end, { desc = "Show / focus / hide the mksk float" })
api.nvim_create_user_command("MkskHide", function() M.hide() end, { desc = "Hide the mksk float" })
api.nvim_create_user_command("MkskQuit", function() M.quit() end, { desc = "Stop mksk and wipe its chat" })
api.nvim_create_user_command("MkskAsk", function(o) M.ask(o.args, o) end,
  { nargs = "*", range = true, desc = "Send a message to mksk ([range] attaches lines)" })
api.nvim_create_user_command("MkskDiff", function()
  local ok, out = pcall(P.show_diff)
  if not ok then vim.notify("mksk: " .. tostring(out), vim.log.levels.WARN) end
end, { desc = "Saved | proposed plan, side by side" })
api.nvim_create_user_command("MkskFile", function()
  local ok, out = pcall(open_file)
  if not ok then vim.notify("mksk: " .. tostring(out), vim.log.levels.WARN) end
end, { desc = "Open the plan file" })
api.nvim_create_user_command("MkskLayout", function(o) M.set_layout(o.args) end, {
  nargs = "?", desc = "mksk float layout",
  complete = function() return LAYOUTS end,
})

local key = vim.g.mksk_toggle_key
if key == nil then key = "<M-m>" end
if key ~= "" then
  vim.keymap.set({ "n", "i", "t" }, key, function() M.toggle() end, { silent = true, desc = "mksk toggle" })
  vim.keymap.set("x", key, function() M.toggle_visual() end, { silent = true, desc = "mksk toggle + attach selection" })
end

package.loaded.mksk = M

-- ---------------------------------------------------------------- Intelligence Network
-- Plug into the shared driver (mimd/inet, or vim.g.inet_dir) as the agent "mksk".
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("mksk", {
      join = function(p)
        if S and S.job then
          send({ t = "net", op = "join", name = p.name, purpose = p.purpose ~= "" and p.purpose or nil,
            hub = p.hub, network = p.network.name })
        else -- not running yet: start the backend already plugged in
          M.start(vim.list_extend(normalize_args(nil), inet.agent_args(p)))
        end
      end,
      leave = function() if S and S.job then send({ t = "net", op = "leave" }) end end,
      status = function() return M.status() end,
    })
  elseif not ok then
    vim.notify("mksk: Intelligence Network driver not loaded (" .. dir .. "/inet.lua): " .. tostring(inet),
      vim.log.levels.WARN)
  end
end

-- `:source mksk.lua` opens the UI right away; `require("mksk")` from a config does not.
if MODNAME == nil and not vim.g.mksk_no_autostart then M.start() end

return M
