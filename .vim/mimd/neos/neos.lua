-- neos.lua: Neovim frontend for the Neos operator agent (requires Neovim >= 0.10).
--
-- The agent runs in neos.py (next to this file); this script draws ONE floating window over
-- ONE persistent chat buffer and executes the agent's editor actions inside your real window.
-- Hiding the float keeps the buffer and the running task; recalling it shows the same buffer
-- from any window, tab or mode.
--
-- Scope: Neos acts only through built-in Ex commands, the Neovim Lua API and the functions/commands
-- defined in ~/.vimrc.neo (neos.py enforces this before anything runs here).
--
--   :source /path/to/neos.lua      start + open (backend args from vim.g.neos_args)
--   :Neos [--port 8080 ...]        open/focus the float, starting the backend with these args
--   :NeosToggle                    show / focus / hide the float (default key <M-n>, also in visual
--                                  mode: attaches the selection to your next message)
--   :[range]NeosAsk [text]         send text (with the range as [SELECTION]) without typing in the float
--   :NeosUvian [keywords]          search Uvian memory and load the top hits into the next question (vimpage: neosu)
--   :NeosLayout center|left|right|top|bottom|full   (no argument: cycle)
--   :NeosDoc                       regenerate agent_doc.md (your keymaps/functions from ~/.vimrc.neo + mini modules;
--                                  also refreshed on start when the rc is newer). Neos searches it to answer
--                                  "how do I ..." questions about your own setup.
--   :NeosQuit                      stop the backend and wipe the chat
--
-- Options: vim.g.neos_python (default "python3"), vim.g.neos_args (list or string),
--          vim.g.neos_toggle_key (default "<M-n>", "" = no global map), vim.g.neos_layout ("center").
--
-- In the float: the last line is the input (a prompt buffer). Everything above is the transcript —
-- normal-mode motions, search and yank work as usual.
--   <CR> send │ ↑/↓ history │ C-x interrupt LLM │ C-c cancel task / clear line │ F2 raw LLM log
--   M-h/M-j/M-k/M-l dock left/bottom/top/right │ M-c center │ M-f full │ M-b peek (translucent) │ q / <Esc> (normal) hide │ <CR> on a "#n uuid" line loads that uvian entry

local MODNAME = ...
local M = {}
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

local SCRIPT_DIR = fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
local HISTORY_FILE = fn.stdpath("data") .. "/neos_history"
local LAYOUTS = { "center", "left", "right", "top", "bottom", "full" }
local MAX_LINES = 10000
local NS = api.nvim_create_namespace("neos")
local NS_PROMPT = api.nvim_create_namespace("neos_prompt")
local SPINNER = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }

local S -- session state; nil when nothing is running

local _doc
local function DOC()
  _doc = _doc or dofile(SCRIPT_DIR .. "/neos_doc.lua")
  return _doc
end

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    NeosBorder = { fg = "#c4c408" },
    NeosBadge = { bg = "#c4c408", fg = "#000000", bold = true },
    NeosTitle = { bg = "#303030", fg = "#d0d0d0" },
    NeosIdle = { bg = "#005f00", fg = "#ffffff", bold = true },
    NeosBusy = { bg = "#af5f00", fg = "#ffffff", bold = true },
    NeosAuto = { bg = "#af0000", fg = "#ffffff", bold = true },
    NeosHint = { fg = "#808080" },
    NeosSteer = { bg = "#5f00af", fg = "#ffffff", bold = true },
    NeosApprove = { bg = "#af0000", fg = "#ffffff", bold = true },
    NeosUser = { fg = "#ffd75f", bold = true },
    NeosAttach = { bg = "#005f87", fg = "#ffffff" },
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

local navmap -- defined with the editor operations below

-- Where the user is: the editor window Neos acts on (tracked on cursor/buffer/window moves, floats ignored).
local function position(win)
  win = win or target_win()
  if not win then return nil end
  local buf = api.nvim_win_get_buf(win)
  local tp = api.nvim_win_get_tabpage(win)
  local wins = {}
  for _, w in ipairs(api.nvim_tabpage_list_wins(tp)) do
    if not is_float(w) then wins[#wins + 1] = w end
  end
  local winnr = 1
  for i, w in ipairs(wins) do if w == win then winnr = i end end
  local cur = api.nvim_win_get_cursor(win)
  local line = api.nvim_buf_get_lines(buf, cur[1] - 1, cur[1], false)[1] or ""
  local alt = fn.bufnr("#")
  return {
    tab = api.nvim_tabpage_get_number(tp), tabs = #api.nvim_list_tabpages(),
    winnr = winnr, wins = #wins, win = win, buf = buf,
    file = fn.fnamemodify(api.nvim_buf_get_name(buf), ":~:."), filetype = vim.bo[buf].filetype,
    buftype = vim.bo[buf].buftype, modified = vim.bo[buf].modified,
    line = cur[1], col = cur[2] + 1, line_count = api.nvim_buf_line_count(buf), text = line:sub(1, 300),
    alt = (alt > 0 and alt ~= buf and api.nvim_buf_is_valid(alt)) and fn.fnamemodify(api.nvim_buf_get_name(alt), ":~:.") or nil,
  }
end

-- ---------------------------------------------------------------- float title / footer
local function title_chunks()
  local st = S.state
  local spin = st.busy and SPINNER[math.floor(uv.hrtime() / 1e8) % #SPINNER + 1] or "●"
  local t = {
    { S.view == "log" and " NEOS · LOG " or " NEOS ", "NeosBadge" },
    { " " .. spin .. " " .. oneline(st.phase or "starting") .. " ", st.busy and "NeosBusy" or "NeosIdle" },
    { " " .. oneline(st.model or "-") .. " ", "NeosTitle" },
  }
  if st.auto then t[#t + 1] = { " auto ", "NeosAuto" } end
  if S.peek then t[#t + 1] = { " peek ", "NeosTitle" } end
  if not S.job then t[#t + 1] = { " backend stopped ", "NeosApprove" } end
  return t
end

local function footer_chunks()
  local st, f = S.state, {}
  local q = {}
  if st.steer and #st.steer > 0 then q[#q + 1] = #st.steer .. " steer" end
  if st.tasks and #st.tasks > 0 then q[#q + 1] = #st.tasks .. " task" end
  if st.loads and #st.loads > 0 then q[#q + 1] = "🧠 " .. #st.loads .. " uvian" end
  if #q > 0 then f[#f + 1] = { " ⏳ " .. table.concat(q, " · ") .. " ", "NeosSteer" } end
  if S.sel then f[#f + 1] = { (" 📎 L%d-%d "):format(S.sel.start, S.sel["end"]), "NeosAttach" } end
  local pos = position()
  if pos then
    local name = fn.fnamemodify(pos.file, ":t")
    f[#f + 1] = { (" ⌖ %s:%d:%d · tab %d/%d · win %d/%d "):format(name ~= "" and name or "[No Name]",
      pos.line, pos.col, pos.tab, pos.tabs, pos.winnr, pos.wins), "NeosTitle" }
  end
  f[#f + 1] = { " F2 log │ M-h/j/k/l dock │ M-c/M-f │ M-b peek │ q hide ", "NeosHint" }
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
  local cfg = float_config()
  pcall(api.nvim_win_set_config, S.win, cfg)
end

-- ---------------------------------------------------------------- prompt line
local function prompt_spec()
  local st = S.state
  if st.approval then
    if st.approval.editing then return { { " EDIT ", "NeosApprove" }, { " ", nil } } end
    return { { " ALLOW? y/n/a/e ", "NeosApprove" }, { " ", nil } }
  end
  if st.busy then return { { " STEER ", "NeosSteer" }, { " !now +task ", "NeosHint" }, { " ", nil } } end
  return { { " NEOS ", "NeosBadge" }, { " ❯ ", "NeosHint" } }
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
-- chat: a prompt buffer — output goes above the prompt (last) line.
-- log : a plain read-only buffer — output goes at the end.
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
        if seg[2] ~= 0 then hls[#hls + 1] = { #lines, s, s + #part, "NeosS" .. seg[2] } end
      end
    end
  end
  if #lines == 1 and lines[1] == "" then return end
  local closed = lines[#lines] == ""
  if closed then table.remove(lines) end

  local is_log = key == "log"
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
  if key == "log" then
    vim.bo[buf].modifiable = true
    api.nvim_buf_set_lines(buf, 0, -1, false, {})
    vim.bo[buf].modifiable = false
  else
    local n = api.nvim_buf_line_count(buf)
    api.nvim_buf_set_lines(buf, 0, n - 1, false, {})
  end
  api.nvim_buf_clear_namespace(buf, NS, 0, -1)
  S.tail[key] = false
end

-- ---------------------------------------------------------------- editor operations (requests from neos.py)
local function snapshot(win, full)
  local buf = api.nvim_win_get_buf(win)
  local info = {
    win = win, buf = buf, file = api.nvim_buf_get_name(buf), filetype = vim.bo[buf].filetype,
    buftype = vim.bo[buf].buftype, modified = vim.bo[buf].modified,
    line_count = api.nvim_buf_line_count(buf), cursor = api.nvim_win_get_cursor(win),
  }
  if not full then return info end
  info.cwd = fn.getcwd(win)
  info.visible = { fn.line("w0", win), fn.line("w$", win) }
  local cur = info.cursor[1]
  local a = math.max(1, math.min(cur - 25, info.visible[1]))
  local b = math.min(info.line_count, math.max(cur + 25, a + 20))
  if b - a > 80 then a, b = math.max(1, cur - 40), math.min(info.line_count, cur + 40) end
  local rows = {}
  for i, l in ipairs(api.nvim_buf_get_lines(buf, a - 1, b, false)) do
    rows[#rows + 1] = ("%d%s %s"):format(a + i - 1, a + i - 1 == cur and ">" or "|", l:sub(1, 300))
  end
  info.text = table.concat(rows, "\n")
  info.buffers = {}
  for _, b2 in ipairs(api.nvim_list_bufs()) do
    if vim.bo[b2].buflisted and #info.buffers < 25 then
      info.buffers[#info.buffers + 1] = { nr = b2, name = fn.fnamemodify(api.nvim_buf_get_name(b2), ":~:."), modified = vim.bo[b2].modified }
    end
  end
  info.capabilities = fn.exists(":AgentCapabilities") == 2
  info.tabs = {}
  local cur_tab = api.nvim_get_current_tabpage()
  for i, tp in ipairs(api.nvim_list_tabpages()) do
    local wins, tcur = {}, api.nvim_tabpage_get_win(tp)
    for _, w in ipairs(api.nvim_tabpage_list_wins(tp)) do
      if not is_float(w) then
        local b = api.nvim_win_get_buf(w)
        wins[#wins + 1] = {
          win = w, buf = b, name = fn.fnamemodify(api.nvim_buf_get_name(b), ":~:."),
          cursor = api.nvim_win_get_cursor(w), modified = vim.bo[b].modified,
          current = (tp == cur_tab and w == win) or (tp ~= cur_tab and w == tcur),
        }
      end
    end
    info.tabs[#info.tabs + 1] = { nr = i, current = tp == cur_tab, wins = wins }
  end
  return info
end

local function pick_buf(p, win)
  local b = tonumber(p.buffer)
  if b and b > 0 then
    if not api.nvim_buf_is_valid(b) then error("buffer " .. b .. " does not exist") end
    if b == S.bufs.chat or b == S.bufs.log then error("buffer " .. b .. " is the Neos chat itself") end
    return b
  end
  return api.nvim_win_get_buf(win)
end

local OPS = {}

function OPS.context(_, win)
  local info = snapshot(win, true)
  info.position = position(win)
  info.nav = navmap(win)
  return info
end

-- Navigation map: every tab and editor window (floats skipped), terminals with their job state.
function navmap(anchor)
  local tabs, user = {}, target_win()
  local cur_tab = api.nvim_get_current_tabpage()
  for i, tp in ipairs(api.nvim_list_tabpages()) do
    local wins = {}
    for _, w in ipairs(api.nvim_tabpage_list_wins(tp)) do
      if not is_float(w) then
        local b = api.nvim_win_get_buf(w)
        local c = api.nvim_win_get_cursor(w)
        local e = {
          win = w, buf = b, user = w == user, anchor = w == anchor, line = c[1], col = c[2] + 1,
          line_count = api.nvim_buf_line_count(b), filetype = vim.bo[b].filetype, modified = vim.bo[b].modified,
          name = fn.fnamemodify(api.nvim_buf_get_name(b), ":~:."), width = api.nvim_win_get_width(w),
          height = api.nvim_win_get_height(w),
        }
        if vim.bo[b].buftype == "terminal" then
          local chan = vim.bo[b].channel
          local pid = vim.b[b].terminal_job_pid
          local alive = chan > 0 and pcall(fn.jobpid, chan)
          local last = ""
          local n = e.line_count
          for _, l in ipairs(api.nvim_buf_get_lines(b, math.max(0, n - 30), n, false)) do
            if l:match("%S") then last = l end
          end
          e.term = { channel = chan, pid = pid, alive = alive, title = vim.b[b].term_title or "", last = last:sub(1, 160) }
        end
        wins[#wins + 1] = e
      end
    end
    tabs[#tabs + 1] = { nr = i, current = tp == cur_tab, wins = wins }
  end
  return tabs
end

-- live placeholder for the backend: the task's anchor window (msg.win), where the user is now, the navigation map
function OPS.position(_, win) return { position = position(win), user = position(target_win()), nav = navmap(win) } end

-- move the user (and Neos's target) to a window or tab
function OPS.focus(p)
  if api.nvim_get_current_win() == S.win then vim.cmd("stopinsert") end
  local w = tonumber(p.win)
  if w then
    if not valid_win(w) or is_float(w) then error("window " .. w .. " does not exist (see [NAV MAP])") end
    api.nvim_set_current_win(w)
  elseif tonumber(p.tab) then
    local tp = api.nvim_list_tabpages()[tonumber(p.tab)]
    if not tp then error("tab " .. p.tab .. " does not exist") end
    api.nvim_set_current_tabpage(tp)
    w = api.nvim_get_current_win()
  else
    error("focus needs \"win\" (winid) or \"tab\" (number)")
  end
  if is_float(w) then w = target_win() end
  S.target = w
  local pos = position(w)
  return ("now in tab %d/%d, window %d/%d (winid %d): %s line %d"):format(pos.tab, pos.tabs, pos.winnr, pos.wins,
    w, pos.file ~= "" and pos.file or "[No Name]", pos.line)
end

-- type into a terminal window, wait a little, return its latest output
function OPS.term(p, win)
  local buf = api.nvim_win_get_buf(win)
  if vim.bo[buf].buftype ~= "terminal" then error("window " .. win .. " is not a terminal (see [NAV MAP])") end
  local chan = vim.bo[buf].channel
  if chan <= 0 or not pcall(fn.jobpid, chan) then error("the terminal job in window " .. win .. " is not running") end
  local keys = tostring(p.keys or "")
  if p.enter ~= false and not keys:match("[\r\n]$") then keys = keys .. "\r" end
  local before = api.nvim_buf_line_count(buf)
  fn.chansend(chan, keys)
  local wait = math.min(tonumber(p.wait) or 1500, 15000)
  vim.wait(wait, function() return false end, 50)
  -- keep the terminal scrolled to the end so the user sees it too
  pcall(api.nvim_win_set_cursor, win, { api.nvim_buf_line_count(buf), 0 })
  local lines = api.nvim_buf_get_lines(buf, 0, -1, false)
  while #lines > 0 and not lines[#lines]:match("%S") do table.remove(lines) end
  local from = math.max(1, math.min(before - 2, #lines - 60))
  local out = {}
  for i = from, #lines do out[#out + 1] = lines[i] end
  return ("sent %q to terminal (winid %d); latest output:\n%s"):format(keys, win, table.concat(out, "\n"))
end

function OPS.ex(p, win)
  local res = api.nvim_win_call(win, function() return api.nvim_exec2(p.command, { output = true }) end)
  return res.output or ""
end

-- Neovim Lua API chunk (already policy-checked by neos.py); returns print() output and return values
function OPS.lua(p, win)
  local chunk, err = load(p.code, "=neos", "t")
  if not chunk then error(err, 0) end
  local printed, orig = {}, _G.print
  _G.print = function(...)
    local t = {}
    for i = 1, select("#", ...) do t[#t + 1] = tostring((select(i, ...))) end
    printed[#printed + 1] = table.concat(t, " ")
  end
  local ok, packed = pcall(api.nvim_win_call, win, function() return table.pack(chunk()) end)
  _G.print = orig
  if not ok then error(packed, 0) end
  for i = 1, packed.n do printed[#printed + 1] = "return[" .. i .. "]: " .. vim.inspect(packed[i]) end
  return table.concat(printed, "\n")
end

function OPS.read(p, win, res)
  local buf = pick_buf(p, win)
  local n = api.nvim_buf_line_count(buf)
  local cur = api.nvim_win_get_buf(win) == buf and api.nvim_win_get_cursor(win)[1] or 1
  local a = math.max(1, tonumber(p.start) or (cur - 20))
  local b = math.min(n, tonumber(p["end"]) or (a + 60))
  if b - a > 500 then b = a + 500 end
  local rows = {}
  for i, l in ipairs(api.nvim_buf_get_lines(buf, a - 1, b, false)) do rows[#rows + 1] = (a + i - 1) .. "| " .. l end
  res.range = ("L%d-%d/%d"):format(a, b, n)
  res.name = fn.fnamemodify(api.nvim_buf_get_name(buf), ":t")
  return ("%s lines %d-%d of %d:\n%s"):format(api.nvim_buf_get_name(buf), a, b, n, table.concat(rows, "\n"))
end

function OPS.edit(p, win)
  local buf = pick_buf(p, win)
  local n = api.nvim_buf_line_count(buf)
  local a, b = tonumber(p.start), tonumber(p["end"])
  if not a or not b then error("start and end must be numbers") end
  if a < 1 or a > n + 1 then error(("start %d out of range 1..%d"):format(a, n + 1)) end
  if b < a - 1 or b > n then error(("end %d out of range %d..%d"):format(b, a - 1, n)) end
  if not vim.bo[buf].modifiable then error("buffer is not modifiable") end
  local lines = {}
  for _, l in ipairs(p.lines or {}) do
    for _, piece in ipairs(vim.split(tostring(l), "\n", { plain = true })) do lines[#lines + 1] = piece end
  end
  api.nvim_buf_set_lines(buf, a - 1, b, false, lines)
  local new_n = api.nvim_buf_line_count(buf)
  local lo, hi = math.max(1, a - 2), math.min(new_n, a + #lines + 1)
  local rows = {}
  for i, l in ipairs(api.nvim_buf_get_lines(buf, lo - 1, hi, false)) do rows[#rows + 1] = (lo + i - 1) .. "| " .. l end
  return ("replaced lines %d-%d with %d line(s); buffer now has %d lines. Around the change:\n%s")
    :format(a, b, #lines, new_n, table.concat(rows, "\n"))
end

local function handle_req(msg)
  local res = { t = "res", id = msg.id }
  local win = target_win()
  local want = tonumber(msg.win)
  if want and msg.op ~= "focus" then -- act in a specific window without moving the user
    if not valid_win(want) or is_float(want) then
      res.ok, res.err = false, "window " .. want .. " does not exist (see [NAV MAP])"
      return send(res)
    end
    win = want
  end
  if not win then
    res.ok, res.err = false, "no editor window to act on"
    return send(res)
  end
  local op = OPS[msg.op]
  if not op then
    res.ok, res.err = false, "unknown op " .. tostring(msg.op)
    return send(res)
  end
  local ok, out = pcall(op, msg, win, res)
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
  wo.winhighlight = "Normal:NormalFloat,FloatBorder:NeosBorder"
  pcall(function() wo.winfixbuf = true end)
end

local function enter_input()
  if not float_shown() then return end
  if S.view ~= "chat" then
    S.view = "chat"
    pcall(function() vim.wo[S.win].winfixbuf = false end)
    api.nvim_win_set_buf(S.win, S.bufs.chat)
    pcall(function() vim.wo[S.win].winfixbuf = true end)
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
  vim.wo[S.win].winblend = S.peek and (vim.g.neos_peek_blend or 70) or 0
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

local function toggle_view()
  if not float_shown() then return end
  S.view = S.view == "chat" and "log" or "chat"
  pcall(function() vim.wo[S.win].winfixbuf = false end)
  api.nvim_win_set_buf(S.win, S.bufs[S.view])
  pcall(function() vim.wo[S.win].winfixbuf = true end)
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
    api.nvim_set_hl(0, "NeosS" .. msg.id, {
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
    if not float_shown() then vim.notify(msg.text, msg.level == "warn" and vim.log.levels.WARN or vim.log.levels.INFO) end
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
  S.state.busy = false
  append("chat", { { "[neos backend exited with code " .. code .. " — :Neos to restart]\n", 0 } })
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

-- Mark a submitted (or NeosAsk) line in the transcript: badge part + user text.
local function mark_user_line(row, plen)
  local ib = S.bufs.chat
  local line = api.nvim_buf_get_lines(ib, row, row + 1, false)[1] or ""
  pcall(api.nvim_buf_set_extmark, ib, NS, row, 0, { end_col = math.min(plen, #line), hl_group = "NeosHint" })
  pcall(api.nvim_buf_set_extmark, ib, NS, row, math.min(plen, #line), { end_col = #line, hl_group = "NeosUser" })
end

local function submit(text, sel)
  S.hidx = nil
  if not S.state.approval then save_history(text) end
  if S.job then
    send({ t = "input", text = text, sel = sel, anchor = target_win() }) -- the task is pinned to this window
  else
    append("chat", { { "backend is not running; :Neos to restart\n", 0 } })
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
  return { file = api.nvim_buf_get_name(buf), buf = buf, start = l1, ["end"] = l2, text = table.concat(rows, "\n") }
end

function M.toggle_visual()
  -- read the selection before anything moves the cursor or focus
  local l1, l2 = fn.line("v"), fn.line(".")
  local buf, win = api.nvim_get_current_buf(), api.nvim_get_current_win()
  vim.cmd('execute "normal! \\<Esc>"')
  if not S or not S.job then M.start() end
  if not S then return end
  if buf ~= S.bufs.chat and buf ~= S.bufs.log then
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
  local cb, lb = S.bufs.chat, S.bufs.log
  local function map(modes, lhs, rhs, bufs)
    for _, b in ipairs(bufs) do vim.keymap.set(modes, lhs, rhs, { buffer = b, nowait = true, silent = true }) end
  end
  local both = { cb, lb }
  map({ "n", "i" }, "<C-x>", function() send({ t = "interrupt" }) end, both)
  map({ "n", "i" }, "<C-c>", function()
    if S.state.busy then send({ t = "cancel" }) else set_input("") end
  end, both)
  map({ "n", "i" }, "<F2>", toggle_view, both)
  -- dock the float to a side so the windows behind it stay visible; M-b makes it translucent (peek)
  map({ "n", "i" }, "<M-h>", function() M.set_layout("left") end, both)
  map({ "n", "i" }, "<M-l>", function() M.set_layout("right") end, both)
  map({ "n", "i" }, "<M-k>", function() M.set_layout("top") end, both)
  map({ "n", "i" }, "<M-j>", function() M.set_layout("bottom") end, both)
  map({ "n", "i" }, "<M-c>", function() M.set_layout("center") end, both)
  map({ "n", "i" }, "<M-f>", function() M.set_layout("full") end, both)
  map({ "n", "i" }, "<M-b>", M.peek, both)
  map("n", "q", M.hide, both)
  map("n", "<Esc>", M.hide, both)
  map("i", "<Up>", function() history_step(-1) end, { cb })
  map("i", "<Down>", function() history_step(1) end, { cb })
  for _, lhs in ipairs({ "i", "a", "I", "A", "o" }) do map("n", lhs, enter_input, both) end
  -- <CR> on "#n <uuid>" loads that uvian entry; elsewhere it goes to the input line
  map("n", "<CR>", function()
    local row = api.nvim_win_get_cursor(0)[1]
    local text = api.nvim_buf_get_lines(0, row - 1, row, false)[1] or ""
    local uid = text:match("^%s*#%d+%s+([%x%-]+)")
    if uid and #uid >= 8 and S.job then
      send({ t = "input", text = "===l " .. uid })
    else
      enter_input()
    end
  end, both)
end

local function set_autocmds()
  local group = api.nvim_create_augroup("Neos", { clear = true })
  api.nvim_create_autocmd("WinClosed", {
    group = group,
    callback = function(ev)
      if S and tonumber(ev.match) == S.win then S.win = nil end
    end,
  })
  api.nvim_create_autocmd("VimResized", { group = group, callback = function() if S then refresh_float() end end })
  -- follow the user's position: the window/buffer/cursor Neos acts on (the float itself is ignored)
  api.nvim_create_autocmd({ "WinEnter", "BufEnter", "TabEnter" }, {
    group = group,
    callback = function()
      if not S then return end
      remember_target()
      if float_shown() then refresh_float() end
    end,
  })
  api.nvim_create_autocmd({ "CursorMoved", "CursorMovedI" }, {
    group = group,
    callback = function()
      if not S or not float_shown() then return end
      local w = api.nvim_get_current_win()
      if w ~= S.win and not is_float(w) then
        S.target = w
        refresh_float()
      end
    end,
  })
  api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function() if S and S.job then send({ t = "quit" }) end end,
  })
end

local function create_buffers()
  local cb = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, cb, "neos://chat")
  vim.bo[cb].buftype, vim.bo[cb].bufhidden, vim.bo[cb].swapfile = "prompt", "hide", false
  vim.bo[cb].filetype = "neos"
  fn.prompt_setcallback(cb, on_submit)
  fn.prompt_setinterrupt(cb, function() send({ t = "interrupt" }) end)
  local lb = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, lb, "neos://log")
  vim.bo[lb].bufhidden, vim.bo[lb].swapfile, vim.bo[lb].modifiable = "hide", false, false
  vim.bo[lb].filetype = "neoslog"
  return { chat = cb, log = lb }
end

local function normalize_args(args)
  if args == nil then args = vim.g.neos_args end
  if type(args) == "string" then args = vim.split(vim.trim(args), "%s+", { trimempty = true }) end
  local out = {}
  for _, a in ipairs(args or {}) do out[#out + 1] = tostring(a) end
  return out
end

-- ---------------------------------------------------------------- public API
function M.start(args)
  define_highlights()
  if S and S.job then return M.show(true) end
  local first = S == nil
  if first then
    local layout = 1
    for i, l in ipairs(LAYOUTS) do if l == vim.g.neos_layout then layout = i end end
    S = {
      bufs = create_buffers(), view = "chat", layout = layout, tail = { chat = false, log = true },
      state = { phase = "starting" }, prompt = "", partial = "", history = load_history(),
    }
    remember_target()
    set_keymaps()
    set_autocmds()
  end
  S.partial = ""
  local py = vim.g.neos_python or "python3"
  local cmd = { py, "-u", SCRIPT_DIR .. "/neos.py" }
  local ok, doc = pcall(function() return DOC().ensure() end) -- agent doc of the user's vim setup
  if ok and doc then
    vim.list_extend(cmd, { "--vim-doc", doc })
  else
    vim.notify("Neos: could not build the vim setup doc: " .. tostring(doc), vim.log.levels.WARN)
  end
  vim.list_extend(cmd, normalize_args(args))
  S.job = fn.jobstart(cmd, { cwd = fn.getcwd(), on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    vim.notify("Neos: failed to start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
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

-- neosu: search Uvian memory and load the top hits into the next question
function M.uvian(query)
  if not S or not S.job then M.start() end
  if not S or not S.job then return end
  local q = vim.trim(query or "")
  if q == "" then
    local ok, ans = pcall(fn.input, { prompt = "Uvian ❯ ", default = vim.trim(input_text()) })
    q = ok and vim.trim(ans or "") or ""
  end
  if q == "" then return end
  send({ t = "input", text = "===u " .. q })
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
  pcall(api.nvim_del_augroup_by_name, "Neos")
  S = nil
end

api.nvim_create_user_command("Neos", function(o) M.start(#o.fargs > 0 and o.fargs or nil) end,
  { nargs = "*", desc = "Open the Neos float (starts the backend)" })
api.nvim_create_user_command("NeosToggle", function() M.toggle() end, { desc = "Show / focus / hide the Neos float" })
api.nvim_create_user_command("NeosHide", function() M.hide() end, { desc = "Hide the Neos float" })
api.nvim_create_user_command("NeosQuit", function() M.quit() end, { desc = "Stop Neos and wipe its chat" })
api.nvim_create_user_command("NeosAsk", function(o) M.ask(o.args, o) end,
  { nargs = "*", range = true, desc = "Send a message to Neos ([range] attaches lines)" })
api.nvim_create_user_command("NeosUvian", function(o) M.uvian(o.args) end,
  { nargs = "*", desc = "Search Uvian memory and load the top hits into the next Neos question" })
api.nvim_create_user_command("NeosDoc", function()
  local out, n, files = DOC().generate()
  vim.notify(("Neos: wrote %s (%d entries from %d file(s))"):format(fn.fnamemodify(out, ":~"), n, files))
end, { desc = "Regenerate the Neos agent doc from ~/.vimrc.neo and its mini modules" })
api.nvim_create_user_command("NeosLayout", function(o) M.set_layout(o.args) end, {
  nargs = "?", desc = "Neos float layout",
  complete = function() return LAYOUTS end,
})

local key = vim.g.neos_toggle_key
if key == nil then key = "<M-n>" end
if key ~= "" then
  vim.keymap.set({ "n", "i", "t" }, key, function() M.toggle() end, { silent = true, desc = "Neos toggle" })
  vim.keymap.set("x", key, function() M.toggle_visual() end, { silent = true, desc = "Neos toggle + attach selection" })
end

package.loaded.neos = M

-- `:source neos.lua` opens the UI right away; `require("neos")` from a config does not.
if MODNAME == nil and not vim.g.neos_no_autostart then M.start() end

return M
