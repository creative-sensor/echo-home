-- mksk.lua: Neovim frontend for mksk, the make-sketch planning agent (requires Neovim >= 0.10).
--
-- Cloned from neos.lua (one persistent chat buffer, the same prompt line, raw LLM log, steering and input history),
-- trimmed to planning, with an IFACE pane for the network. mksk lives in a tab of its own:
--
--   ┌──────────────────────────────────┬────────────────────┐
--   │ plan file (the plan mksk works on)│ REVIEW             │  REVIEW: only while changes reported on the
--   │                                  │                    │  network wait for you (y apply, n drop, v diff)
--   ├──────────────────────┬───────────┴──────┬─────────────┤
--   │ chat (input: last    │ raw LLM log      │ IFACE       │  IFACE: only while mksk is on the network
--   │ line)                │                  │             │
--   └──────────────────────┴──────────────────┴─────────────┘
--
-- The agent runs in mksk.py (next to this file). It plans with you and keeps a plan file in the make-sketch format
-- (mksk--<plan_name>--YYYY-MM-DD.md, ~/echo-home/Documents/specs/make-sketch.md). At start it opens the most
-- up-to-date plan file in the current directory, else a zero plan. Every read and edit of the plan goes through
-- the built-in functions of mksk_plan.lua (weights, table, targets, notes, diff, task brief), run here on the agent's
-- request. Edits are staged: nothing is written until you approve the diff, and then only to today's file. The chat
-- stays in the agent's context from start to quit. Task assignment exists only while mksk is on the network.
--
--   :source /path/to/mksk.lua        start + open the mksk tab (backend args from vim.g.mksk_args)
--   :Mksk [plan] [--port 8080 ...]   open the mksk tab and focus the chat, starting the backend (a plan name overrides
--                                    the newest plan; a new name starts a new plan)
--   :MkskToggle                      focus the chat / back to the previous tab (default key <M-m>, also in visual
--                                    mode: attaches the selection to your next message)
--   :MkskHide                        back to the previous tab
--   :[range]MkskAsk [text]           send text (with the range as [SELECTION]) without typing in the chat
--   :MkskDiff                        saved | proposed plan, side by side in a tab (q closes it)
--   :MkskFile                        focus the plan pane (and show the plan file there again if you left it)
--   :MkskReview                      focus the REVIEW pane (also ===p in the chat)
--   :MkskQuit                        stop the backend, wipe the chat and close the mksk tab
--   :InetJoin mksk / :InetLeave mksk join / leave the Intelligence Network (profile g:inet_agents.mksk, optional)
--
-- Options: vim.g.mksk_python (default "python3", "python" on Windows), vim.g.mksk_args (list or string),
--          vim.g.mksk_toggle_key (default "<M-m>", "" = no global map),
--          vim.g.mksk_dir (plan folder; default: the current directory when mksk starts), vim.g.mksk_plan.
--
-- In the chat pane: the last line is the input (a prompt buffer). Everything above is the transcript —
-- normal-mode motions, search and yank work as usual.
--   <CR> send │ ↑/↓ history │ C-x interrupt LLM │ C-c cancel request / clear line │ q (normal) back to the previous tab
--   <CR> (normal) on a numbered option picks it │ i/a/o (normal, also in the other panes) go to the input
-- In the REVIEW pane (each change: who reported it, the report, its edits, and its diff against the saved plan):
--   y apply it to the plan file │ n drop it │ v or <CR> saved | with it, side by side │ ]] / [[ next / previous change
--   (applying waits for the running request; the reporter is told the verdict; an assignment asked on the network
--   shows "✉ y also sends @agent the brief of <target>": y saves it and sends the brief)
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
local MAX_LINES = 10000
local NS = api.nvim_create_namespace("mksk")
local NS_PROMPT = api.nvim_create_namespace("mksk_prompt")
local NS_REVIEW = api.nvim_create_namespace("mksk_review")
local PANES = { "chat", "log", "iface", "queue" } -- the panes mksk owns (the plan pane shows the plan file)
local PANE_TITLE = { chat = " MKSK ", log = " LLM ", iface = " MKSK · IFACE ", queue = " MKSK · REVIEW " }
local PANE_HL = { chat = "Chat", log = "Log", iface = "Iface", queue = "Queue" } -- each pane's bar colors (MkskBadge*/MkskBar*)
local BOTTOM = 0.45 -- share of the tab height the chat row gets when the tab is laid out
local REVIEW_WIDTH = 0.38 -- share of the tab width the REVIEW pane gets
local REVIEW_DIFF = 14 -- diff lines shown per change in the REVIEW pane (v shows everything)

local S -- session state; nil when nothing is running
local render_queue -- the REVIEW pane (defined below)

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    MkskBadge = { bg = "#08a4c4", fg = "#000000", bold = true },
    MkskTitle = { bg = "#303030", fg = "#d0d0d0" },
    MkskIdle = { bg = "#005f00", fg = "#ffffff", bold = true },
    MkskBusy = { bg = "#af5f00", fg = "#ffffff", bold = true },
    MkskHint = { fg = "#808080" },
    MkskSteer = { bg = "#5f00af", fg = "#ffffff", bold = true },
    MkskApprove = { bg = "#af0000", fg = "#ffffff", bold = true },
    MkskUser = { fg = "#5fd7ff", bold = true },
    MkskAttach = { bg = "#005f87", fg = "#ffffff" },
    MkskReviewHead = { fg = "#ffd75f", bold = true },
  }
  -- Each pane's header has its own color: the title badge + a tinted strip (the rest of the bar), as hbmx's panes.
  -- chat keeps mksk's cyan; PLAN green, LOG blue, IFACE purple, REVIEW orange (it waits for you).
  -- Override with :hi MkskBarLog ... / MkskBadgeLog ...
  local light = vim.o.background == "light"
  local on = light and "#fbf1c7" or "#1d2021"
  local panes = {
    Chat = { badge = "#08a4c4", fg = "#000000", bar = light and "#cdeaf0" or "#1f3a40" },
    Log = { badge = light and "#076678" or "#83a598", fg = on, bar = light and "#cadbea" or "#2a3b48" },
    Iface = { badge = light and "#8f3f71" or "#d3869b", fg = on, bar = light and "#e7cfdc" or "#43304a" },
    Queue = { badge = light and "#af3a03" or "#fe8019", fg = on, bar = light and "#f2d9c4" or "#4a3326" },
    Plan = { badge = light and "#79740e" or "#b8bb26", fg = on, bar = light and "#e0e5c3" or "#353a1f" },
  }
  for name, p in pairs(panes) do
    hl["MkskBadge" .. name] = { bg = p.badge, fg = p.fg, bold = true }
    hl["MkskBar" .. name] = { bg = p.bar }
  end
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
end

-- ---------------------------------------------------------------- helpers
local function valid_win(w) return w ~= nil and api.nvim_win_is_valid(w) end
local function is_float(w) return api.nvim_win_get_config(w).relative ~= "" end
local function oneline(s) return (tostring(s or ""):gsub("[\r\n]+", " ")) end
local function clean_dir(d) return (native(fn.fnamemodify(fn.expand(d), ":p")):gsub("[/\\]$", "")) end

local function same_path(a, b)
  if not a or not b or a == "" or b == "" then return false end
  a, b = native(fn.fnamemodify(a, ":p")), native(fn.fnamemodify(b, ":p"))
  if fn.has("win32") == 1 then a, b = a:lower(), b:lower() end
  return a == b
end

local function send(msg)
  if S and S.job then pcall(fn.chansend, S.job, vim.json.encode(msg) .. "\n") end
end

local function tab_valid() return S.tab ~= nil and api.nvim_tabpage_is_valid(S.tab) end

--- w is a window of the mksk tab.
local function in_tab(w) return valid_win(w) and tab_valid() and api.nvim_win_get_tabpage(w) == S.tab end

--- The mksk tab is the current tab.
local function shown() return S ~= nil and tab_valid() and api.nvim_get_current_tabpage() == S.tab end

--- b is the chat, the log, the IFACE or the REVIEW buffer (not the plan).
local function is_pane_buf(b) return b == S.bufs.chat or b == S.bufs.log or b == S.bufs.iface or b == S.bufs.queue end
local function is_pane(w) return is_pane_buf(api.nvim_win_get_buf(w)) end

local function target_win()
  if valid_win(S.target) and not is_float(S.target) and not is_pane(S.target) then return S.target end
  if valid_win(S.wins.plan) then
    S.target = S.wins.plan
    return S.target
  end
  for _, w in ipairs(api.nvim_tabpage_list_wins(0)) do
    if not is_float(w) and not is_pane(w) then
      S.target = w
      return w
    end
  end
end

local function remember_target()
  local w = api.nvim_get_current_win()
  if not is_float(w) and not is_pane(w) then S.target = w end
end

--- Where the user is (the last editor window, mksk's panes ignored): lets "this target" refer to the plan file there.
local function position(win)
  win = win or target_win()
  if not win then return nil end
  local buf = api.nvim_win_get_buf(win)
  if buf == S.bufs.plan then return nil end -- the placeholder of a plan with no file yet
  local cur = api.nvim_win_get_cursor(win)
  return { file = native(api.nvim_buf_get_name(buf)), filetype = vim.bo[buf].filetype, line = cur[1], col = cur[2] + 1,
    text = (api.nvim_buf_get_lines(buf, cur[1] - 1, cur[1], false)[1] or ""):sub(1, 300) }
end

-- ---------------------------------------------------------------- pane headers
-- Each pane's bar is its header (the window's winbar, at the top: it stays in view however far the pane scrolls),
-- tinted with the pane's color: the badge, then the status chunks on the pane's strip.
-- No animation: the bar changes only when the phase does (busy = orange, idle = green). A spinner redrawn on a timer
-- made the status bars of the bottom panes flicker for as long as the LLM worked.
local function phase_chunk()
  local st = S.state
  local phase = oneline(st.phase or "starting")
  if st.busy and (phase == "idle" or phase == "") then phase = "working" end -- busy before the first phase arrives
  return { " ● " .. phase .. " ", st.busy and "MkskBusy" or "MkskIdle" }
end

local function net_chunk()
  local net = S.state.net or {}
  if not net.joined then return { " 🌐 off ", "MkskTitle" } end
  return { " 🌐 @" .. oneline(net.name) .. " " .. (net.connected and oneline(net.state) or "connecting") .. " ", "MkskTitle" }
end

--- left (the first two chunks are kept when the line is too long), right
local function pane_chunks(key)
  local st = S.state
  local left, right = { { PANE_TITLE[key], "MkskBadge" .. PANE_HL[key] } }, {}
  if key == "queue" then
    local n = #S.queue
    left[2] = { (" %d waiting "):format(n), n > 0 and "MkskApprove" or "MkskTitle" }
    right[1] = { " y apply │ n drop │ v side by side │ ]] next ", "MkskHint" }
    return left, right
  end
  if key == "iface" then
    left[2] = net_chunk()
    local net = st.net or {}
    if net.joined and net.network then
      right[1] = { " " .. oneline(net.network) .. (net.members and (" · " .. net.members .. " member(s)") or "") .. " ",
        "MkskTitle" }
    end
  elseif key == "chat" then -- the chat's header names the work only while there is some (no "idle", no model)
    if st.busy then left[2] = phase_chunk() end
  else
    left[2] = phase_chunk()
    left[3] = { " " .. oneline(fn.fnamemodify(tostring(st.model or "-"), ":t")) .. " ", "MkskTitle" } -- the file, not its path
  end
  if not S.job then left[#left + 1] = { " backend stopped ", "MkskApprove" } end
  if key == "chat" then
    local q = {}
    if st.steer and #st.steer > 0 then q[#q + 1] = #st.steer .. " steer" end
    if st.tasks and #st.tasks > 0 then q[#q + 1] = #st.tasks .. " task" end
    if #q > 0 then right[#right + 1] = { " ⏳ " .. table.concat(q, " · ") .. " ", "MkskSteer" } end
    if (st.deferred or 0) > 0 then right[#right + 1] = { " ⏸ " .. st.deferred .. " in REVIEW ", "MkskApprove" } end
    if S.sel then right[#right + 1] = { (" 📎 L%d-%d "):format(S.sel.start, S.sel["end"]), "MkskAttach" } end
    if st.plan then
      right[#right + 1] = { " ▤ " .. oneline(st.plan) .. (st.staged and " · staged" or "") .. " ", "MkskTitle" }
    end
    right[#right + 1] = net_chunk()
    right[#right + 1] = { " q hide ", "MkskHint" }
  end
  return left, right
end

local function sl(chunks, i, j)
  local out = {}
  for k = i or 1, j or #chunks do
    local c = chunks[k]
    if c then out[#out + 1] = "%#" .. c[2] .. "#" .. (c[1]:gsub("%%", "%%%%")) end
  end
  return table.concat(out)
end

local function header(key)
  local left, right = pane_chunks(key)
  return sl(left, 1, 2) .. "%<" .. sl(left, 3) .. "%*%=" .. sl(right) .. "%*"
end

--- The plan pane's header: the plan, what waits on it, then the file the pane shows (%t%m: live) — the placeholder of
--- a zero plan has no file. 'winbar' is kept per buffer in a window: BufWinEnter re-applies it (an :edit there).
local function plan_header(w)
  local st = S.state
  local chunks = { { " MKSK · PLAN ", "MkskBadgePlan" }, { " ▤ " .. oneline(st.plan or "zero plan") .. " ", "MkskTitle" } }
  if st.staged then chunks[#chunks + 1] = { " staged edits · :MkskDiff ", "MkskSteer" } end
  if (st.deferred or 0) > 0 then chunks[#chunks + 1] = { " ⏸ " .. st.deferred .. " in REVIEW ", "MkskApprove" } end
  local file = api.nvim_win_get_buf(w) ~= S.bufs.plan and "%#MkskBarPlan# %t%m " or ""
  return sl(chunks) .. file .. "%<%*"
end

local function refresh()
  if not S then return end
  local pw = S.wins.plan
  if in_tab(pw) then
    pcall(function()
      vim.wo[pw].winbar = plan_header(pw)
      local whl = "WinBar:MkskBarPlan,WinBarNC:MkskBarPlan"
      if vim.wo[pw].winhighlight ~= whl then vim.wo[pw].winhighlight = whl end
    end)
  end
  for _, key in ipairs(PANES) do
    local w = S.wins[key]
    if valid_win(w) then
      pcall(function()
        vim.wo[w].winbar = header(key)
        if vim.wo[w].statusline ~= "" then vim.wo[w].statusline = "" end -- an older mksk put the bar there
        local whl = ("WinBar:MkskBar%s,WinBarNC:MkskBar%s"):format(PANE_HL[key], PANE_HL[key])
        if vim.wo[w].winhighlight ~= whl then vim.wo[w].winhighlight = whl end -- the strip takes the pane's color
      end)
    end
  end
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
  local w = S.wins.chat
  if valid_win(w) and api.nvim_win_get_buf(w) == ib then api.nvim_win_set_cursor(w, { n, #S.prompt + #text }) end
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

-- ---------------------------------------------------------------- plan pane
local function plan_text()
  local ok, sum = pcall(P.summary)
  if not ok or sum == "no plan is open" then return { "", "  mksk is opening the plan…" } end
  return { "", "  " .. sum, "", "  The plan file shows here once the first change is approved." }
end

--- The plan pane shows the plan file on disk, or a placeholder while the plan has no file yet. It is switched only
--- when the plan file changes (another plan, a new day's snapshot, the first save) or when forced.
local function show_plan(force)
  local ok, file, stamp = pcall(P.file)
  file = ok and file or nil
  if ok and stamp ~= S.base_stamp then -- the saved plan changed: the REVIEW diffs are against it
    S.base_stamp = stamp
    if #S.queue > 0 then render_queue() end
  end
  local changed = force or file ~= S.plan_file
  S.plan_file = file
  if not file then
    local b = S.bufs.plan
    vim.bo[b].modifiable = true
    api.nvim_buf_set_lines(b, 0, -1, false, plan_text())
    vim.bo[b].modifiable = false
  end
  local w = S.wins.plan
  if not changed or not in_tab(w) then return end
  pcall(function() vim.wo[w].winfixbuf = false end)
  if not file then
    api.nvim_win_set_buf(w, S.bufs.plan)
  elseif not same_path(api.nvim_buf_get_name(api.nvim_win_get_buf(w)), file) then
    local ok2, err = pcall(api.nvim_win_call, w, function()
      vim.cmd("silent keepalt hide edit " .. fn.fnameescape(file))
    end)
    if not ok2 then vim.notify("mksk: cannot show the plan file: " .. tostring(err), vim.log.levels.WARN) end
  end
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

--- Show the plan file in the plan pane again; focus: also go to the mksk tab and into the plan pane.
local function open_file(focus)
  local file = P.file()
  if not file then
    error(P.summary() == "no plan is open" and "no plan is open"
      or "the plan has no file yet (nothing was approved so far)", 0)
  end
  if not S then
    vim.cmd("tabnew " .. fn.fnameescape(file))
    return "opened " .. file .. " in a new tab"
  end
  if focus then M.show(false) end
  show_plan(true)
  if focus and in_tab(S.wins.plan) then
    vim.cmd("stopinsert")
    api.nvim_set_current_win(S.wins.plan)
  end
  return "the plan file is shown in the plan pane of the mksk tab: " .. file
end

function OPS.open_file() return open_file(false) end

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
  show_plan() -- the plan file may have changed (opened, named, saved)
  refresh()
end

-- ---------------------------------------------------------------- the mksk tab
--- top: a pane of the top row (its height follows the plan pane's)
local function setup_pane(win, top)
  local wo = vim.wo[win]
  wo.wrap, wo.linebreak, wo.number, wo.relativenumber = true, true, false, false
  wo.signcolumn, wo.foldcolumn, wo.cursorline, wo.list, wo.spell = "no", "0", false, false, false
  wo.winfixheight = not top
  pcall(function() wo.winfixbuf = true end)
end

local function to_bottom(win, buf) api.nvim_win_set_cursor(win, { api.nvim_buf_line_count(buf), 0 }) end

--- The IFACE pane, right of the log, while mksk is on the network.
local function open_iface()
  local W = S.wins
  if in_tab(W.iface) then return end
  local left = (in_tab(W.log) and W.log) or (in_tab(W.chat) and W.chat) or nil
  if not left then return end
  W.iface = api.nvim_open_win(S.bufs.iface, false,
    { split = "right", win = left, width = math.max(20, math.floor(api.nvim_win_get_width(left) / 2)) })
  setup_pane(W.iface)
  to_bottom(W.iface, S.bufs.iface)
end

local function close_iface()
  if valid_win(S.wins.iface) then pcall(api.nvim_win_close, S.wins.iface, true) end
  S.wins.iface = nil
end

local function sync_net()
  local joined = (S.state.net or {}).joined == true
  if joined == S.net then return end
  S.net = joined
  if joined then open_iface() else close_iface() end
end

-- ---------------------------------------------------------------- REVIEW pane
-- The changes agents reported on the network (S.queue, from mksk.py) wait here for the user: each one with who
-- reported it, the report, its edits, and its diff against the saved plan (P.preview, nothing is staged by it).
-- y / n become a verdict for mksk.py, which applies (or drops) the change before any other request.

local function op_text(op)
  local name, args = tostring(op[1]), op[2] or {}
  local out = { name }
  if args.target then out[2] = (args.sub and (args.sub .. "/") or "") .. tostring(args.target) end
  local keys = vim.tbl_keys(args)
  table.sort(keys)
  for _, k in ipairs(keys) do
    if k ~= "target" and not (k == "sub" and args.target) then
      local v = args[k]
      out[#out + 1] = k .. "=" .. oneline(type(v) == "table" and vim.json.encode(v) or tostring(v))
    end
  end
  return table.concat(out, " ")
end

function render_queue()
  if not S then return end
  local b = S.bufs.queue
  local lines, hls, rows, heads = {}, {}, {}, {}
  local function add(text, hl, qid)
    lines[#lines + 1] = text
    rows[#lines] = qid
    if hl then hls[#hls + 1] = { #lines - 1, hl } end
  end
  if #S.queue == 0 then add("  nothing is waiting for review", "MkskHint") end
  for k, it in ipairs(S.queue) do
    local q = it.qid
    if k > 1 then add("") end
    local busy = it.state == "applying" and " · ⏳ applying" or it.state == "dropping" and " · ⏳ dropping" or ""
    add(("▌#%s @%s · %s%s"):format(q, oneline(it.from), oneline(it.at), busy), "MkskReviewHead", q)
    heads[#heads + 1] = #lines
    for i, l in ipairs(vim.split(tostring(it.text or ""), "\n", { trimempty = true })) do
      if i > 4 then
        add("  …", "MkskHint", q)
        break
      end
      add("  " .. l, nil, q)
    end
    if it.error then add("  ⚠ not saved: " .. oneline(it.error), "WarningMsg", q) end
    for _, op in ipairs(it.ops or {}) do add("  · " .. op_text(op), "MkskHint", q) end
    for _, s in ipairs(it.sends or {}) do
      add(("  ✉ y also sends @%s the brief of %s%s"):format(oneline(s.to), oneline(s.target),
        s.message and s.message ~= vim.NIL and (" + “" .. oneline(s.message) .. "”") or ""), "MkskReviewHead", q)
    end
    local ok, d = pcall(P.preview, { ops = it.ops or {} })
    if not ok then
      add("  ✗ no longer fits the plan: " .. oneline(d), "ErrorMsg", q)
    elseif not d.changed then
      add("  = already in the plan", "MkskHint", q)
    else
      -- changed lines only, padding squeezed; left out: "updated:" (changes with every edit), table rules, and
      -- -/+ pairs that are the same once squeezed (a column got wider, a row moved with its weight)
      local list, cnt = {}, { ["-"] = {}, ["+"] = {} }
      for l in d.text:gmatch("[^\n]+") do
        local sign = l:sub(1, 1)
        local body = vim.trim((l:sub(2):gsub("%s%s+", " ")))
        if (sign == "+" or sign == "-") and body ~= "" and not body:match("^updated:") and not body:match("^|[%-|: ]+|$") then
          list[#list + 1] = { sign, body }
          cnt[sign][body] = (cnt[sign][body] or 0) + 1
        end
      end
      local skip = { ["-"] = {}, ["+"] = {} }
      for body, n in pairs(cnt["-"]) do
        local same = math.min(n, cnt["+"][body] or 0)
        if same > 0 then skip["-"][body], skip["+"][body] = same, same end
      end
      local shown, more = 0, 0
      for _, e in ipairs(list) do
        local sign, body = e[1], e[2]
        if (skip[sign][body] or 0) > 0 then
          skip[sign][body] = skip[sign][body] - 1
        elseif shown < REVIEW_DIFF then
          add("  " .. sign .. " " .. body, sign == "+" and "Added" or "Removed", q)
          shown = shown + 1
        else
          more = more + 1
        end
      end
      if shown == 0 then add("  (only layout changes: v shows them)", "MkskHint", q) end
      if more > 0 then add(("  … %d more line(s): v shows everything"):format(more), "MkskHint", q) end
    end
  end
  -- keep the cursor on the change it was on
  local w = S.wins.queue
  local row, keep
  if valid_win(w) then
    row = api.nvim_win_get_cursor(w)[1]
    keep = S.qrows and S.qrows[row]
  end
  vim.bo[b].modifiable = true
  api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].modifiable = false
  api.nvim_buf_clear_namespace(b, NS_REVIEW, 0, -1)
  for _, h in ipairs(hls) do
    pcall(api.nvim_buf_set_extmark, b, NS_REVIEW, h[1], 0, { end_col = #lines[h[1] + 1], hl_group = h[2] })
  end
  S.qrows, S.qheads = rows, heads
  if valid_win(w) then
    local to = math.min(row, #lines)
    for _, r in ipairs(heads) do
      if keep and rows[r] == keep then to = r end
    end
    pcall(api.nvim_win_set_cursor, w, { to, 0 })
  end
end

local function open_review()
  local W = S.wins
  if in_tab(W.queue) or not in_tab(W.plan) then return end
  W.queue = api.nvim_open_win(S.bufs.queue, false,
    { split = "right", win = W.plan, width = math.max(40, math.floor(vim.o.columns * REVIEW_WIDTH)) })
  setup_pane(W.queue, true)
end

local function close_review()
  if valid_win(S.wins.queue) then pcall(api.nvim_win_close, S.wins.queue, true) end
  S.wins.queue = nil
end

--- The REVIEW pane is shown while something waits for review.
local function sync_review()
  if #S.queue > 0 then open_review() else close_review() end
end

local function queued_at_cursor()
  local qid = S.qrows and S.qrows[api.nvim_win_get_cursor(0)[1]]
  for _, it in ipairs(S.queue) do
    if it.qid == qid then return it end
  end
end

local function verdict(op)
  local it = queued_at_cursor()
  if not it then return vim.notify("mksk: no queued change under the cursor", vim.log.levels.WARN) end
  if not S.job then return vim.notify("mksk: the backend is not running (:Mksk restarts it)", vim.log.levels.WARN) end
  if it.state ~= "waiting" then return end
  send({ t = "queue", op = op, qid = it.qid })
end

local function review_side_by_side()
  local it = queued_at_cursor()
  if not it then return vim.notify("mksk: no queued change under the cursor", vim.log.levels.WARN) end
  local ok, err = pcall(P.show_diff, { ops = it.ops or {}, label = "queued-" .. it.qid })
  if not ok then vim.notify("mksk: " .. tostring(err), vim.log.levels.WARN) end
end

local function review_jump(dir)
  local row, heads = api.nvim_win_get_cursor(0)[1], S.qheads or {}
  local to
  for _, r in ipairs(heads) do
    if dir > 0 and r > row and not to then to = r end
    if dir < 0 and r < row then to = r end
  end
  if to then api.nvim_win_set_cursor(0, { to, 0 }) end
end

--- Lay out the mksk tab (the current tab): the plan on top, chat | log (| IFACE) below. Recreates closed panes.
local function build()
  local W = S.wins
  local fresh = not in_tab(W.chat) and not in_tab(W.log)
  if not in_tab(W.plan) then
    W.plan = api.nvim_open_win(S.bufs.plan, false, { split = "above", win = -1 })
    vim.wo[W.plan].winfixheight = false
    show_plan(true)
  end
  if not in_tab(W.chat) then
    if in_tab(W.log) then
      W.chat = api.nvim_open_win(S.bufs.chat, false, { split = "left", win = W.log })
    else
      W.chat = api.nvim_open_win(S.bufs.chat, false, { split = "below", win = -1 })
    end
    setup_pane(W.chat)
    to_bottom(W.chat, S.bufs.chat)
  end
  if not in_tab(W.log) then
    W.log = api.nvim_open_win(S.bufs.log, false, { split = "right", win = W.chat })
    setup_pane(W.log)
    to_bottom(W.log, S.bufs.log)
  end
  if S.net then open_iface() end
  if #S.queue > 0 then open_review() end
  if fresh then
    local total = api.nvim_win_get_height(W.plan) + api.nvim_win_get_height(W.chat) + 1
    pcall(api.nvim_win_set_height, W.chat, math.max(6, math.floor(total * BOTTOM)))
    pcall(api.nvim_win_set_width, W.chat, math.floor(vim.o.columns * (in_tab(W.iface) and 0.4 or 0.5)))
  end
  refresh()
end

local function open_tab()
  vim.cmd("tabnew")
  S.tab = api.nvim_get_current_tabpage()
  local scratch = api.nvim_get_current_buf()
  S.wins = { plan = api.nvim_get_current_win() }
  show_plan(true)
  -- the empty buffer tabnew made, unless :edit reused it for the plan file
  if api.nvim_buf_is_valid(scratch) and scratch ~= api.nvim_win_get_buf(S.wins.plan)
    and api.nvim_buf_get_name(scratch) == "" and not vim.bo[scratch].modified then
    pcall(api.nvim_buf_delete, scratch, {})
  end
end

local function enter_input()
  local w = S and S.wins.chat
  if not shown() or not in_tab(w) then return end
  api.nvim_set_current_win(w)
  local n = api.nvim_buf_line_count(S.bufs.chat)
  api.nvim_win_set_cursor(w, { n, #api.nvim_buf_get_lines(S.bufs.chat, n - 1, n, false)[1] })
  vim.cmd("startinsert!")
end

--- Go to the mksk tab (opening or repairing it); enter: focus the chat input.
function M.show(enter)
  if not S then return M.start() end
  if enter ~= false then remember_target() end
  if not tab_valid() then
    open_tab()
  elseif api.nvim_get_current_tabpage() ~= S.tab then
    api.nvim_set_current_tabpage(S.tab)
  end
  build()
  if enter ~= false then enter_input() end
end

--- Back to the tab you came from (the mksk tab stays as it is).
function M.hide()
  if not shown() then return end
  vim.cmd("stopinsert")
  local back = S.prev_tab
  if back and back ~= S.tab and api.nvim_tabpage_is_valid(back) then
    api.nvim_set_current_tabpage(back)
  elseif #api.nvim_list_tabpages() > 1 then
    vim.cmd("tabprevious")
  else
    vim.notify("mksk: no other tab to go back to", vim.log.levels.INFO)
  end
end

function M.toggle()
  if not S then return M.start() end
  if shown() and api.nvim_get_current_win() == S.wins.chat then return M.hide() end
  M.show(true)
end

--- Go to the REVIEW pane.
function M.review()
  if not S then return vim.notify("mksk is not running", vim.log.levels.WARN) end
  if #S.queue == 0 then return vim.notify("mksk: nothing is waiting for review", vim.log.levels.INFO) end
  M.show(false)
  open_review()
  if in_tab(S.wins.queue) then
    vim.cmd("stopinsert")
    api.nvim_set_current_win(S.wins.queue)
  end
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
    sync_net()
    show_plan()
    refresh()
  elseif t == "req" then
    vim.schedule(function() if S then handle_req(msg) end end)
  elseif t == "clear" then
    clear_buf(msg.b)
  elseif t == "queue" then
    S.queue = msg.items or {}
    render_queue()
    sync_review()
    refresh()
  elseif t == "queue_show" then
    M.review()
  elseif t == "show" then -- an answer is needed: focus the input, but never pull you out of another tab
    if shown() then
      enter_input()
    else
      vim.notify("mksk is waiting for your answer (:MkskToggle)", vim.log.levels.WARN)
    end
  elseif t == "input" then
    set_input(oneline(msg.text))
    if shown() then enter_input() end
  elseif t == "notify" then
    if not shown() then vim.notify("mksk: " .. msg.text, msg.level == "warn" and vim.log.levels.WARN or vim.log.levels.INFO) end
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
    append("chat", { { "backend error (see the log pane): " .. oneline(text):sub(1, 200) .. "\n", 0 } })
  end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  S.state.busy, S.state.approval = false, nil
  pcall(P.discard) -- edits the backend staged are orphaned now
  append("chat", { { "[mksk backend exited with code " .. code .. " — :Mksk to restart]\n", 0 } })
  update_prompt()
  refresh()
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
  vim.schedule(function() if S then update_prompt() refresh() end end)
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
  if not is_pane_buf(buf) then
    S.sel = capture_selection(buf, l1, l2)
    if not is_float(win) then S.target = win end
  end
  if not shown() or api.nvim_get_current_win() ~= S.wins.chat then M.show(true) end
  refresh()
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
  if not shown() then M.show(false) end
end

-- ---------------------------------------------------------------- keymaps / autocmds
local function set_keymaps()
  local cb, lb, ib, qb = S.bufs.chat, S.bufs.log, S.bufs.iface, S.bufs.queue
  local function map(modes, lhs, rhs, bufs)
    for _, b in ipairs(bufs) do vim.keymap.set(modes, lhs, rhs, { buffer = b, nowait = true, silent = true }) end
  end
  local all = { cb, lb, ib, qb }
  map("n", "y", function() verdict("apply") end, { qb })
  map("n", "n", function() verdict("drop") end, { qb })
  map("n", "v", review_side_by_side, { qb })
  map("n", "]]", function() review_jump(1) end, { qb })
  map("n", "[[", function() review_jump(-1) end, { qb })
  map({ "n", "i" }, "<C-x>", function() send({ t = "interrupt" }) end, all)
  map({ "n", "i" }, "<C-c>", function()
    if S.state.busy then send({ t = "cancel" }) else set_input("") end
  end, all)
  map("n", "q", M.hide, all)
  map("i", "<Up>", function() history_step(-1) end, { cb })
  map("i", "<Down>", function() history_step(1) end, { cb })
  for _, lhs in ipairs({ "i", "a", "I", "A", "o" }) do map("n", lhs, enter_input, all) end
  -- <CR> on "  2. option" while mksk waits for a pick answers with that number; elsewhere it goes to the input line
  map("n", "<CR>", function()
    if api.nvim_get_current_buf() == qb then return review_side_by_side() end
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
      if not S then return end
      local id = tonumber(ev.match)
      for k, w in pairs(S.wins) do
        if w == id then S.wins[k] = nil end
      end
    end,
  })
  -- the tab to go back to when you leave the mksk tab
  api.nvim_create_autocmd("TabLeave", {
    group = group,
    callback = function()
      local t = api.nvim_get_current_tabpage()
      if S and t ~= S.tab then S.prev_tab = t end
    end,
  })
  -- follow the user's position: the window "this target" refers to (mksk's panes are ignored)
  api.nvim_create_autocmd({ "WinEnter", "BufEnter", "TabEnter" }, {
    group = group,
    callback = function() if S then remember_target() end end,
  })
  -- a plan file saved by hand: the plan pane and the REVIEW diffs follow it
  api.nvim_create_autocmd("BufWritePost", {
    group = group,
    pattern = "mksk--*.md",
    callback = function() if S then show_plan() end end,
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
  -- plan: what the plan pane shows while the plan has no file yet
  for key, ft in pairs({ log = "mksklog", iface = "mkskiface", plan = "mkskplan", queue = "mkskreview" }) do
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
  -- "--port gml8" (or gml8:vision, gml8:<name>): the LLM service gml8 provides, found on its bus (gml8bus.lua);
  -- none up: g:LMSH_PORT, else 8080
  local ok, bus = pcall(function()
    return package.loaded.gml8bus or dofile(fn.fnamemodify(SCRIPT_DIR, ":h") .. "/gmlama/gml8bus.lua")
  end)
  if ok and type(bus) == "table" and bus.resolve_args then out = bus.resolve_args(out, { by = "mksk" }) end
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
    S = {
      bufs = create_buffers(), wins = {}, tail = { chat = false, log = true, iface = true }, queue = {},
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
  M.show(true)
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  if tab_valid() and #api.nvim_list_tabpages() > 1 then
    if shown() then vim.cmd("stopinsert") end
    pcall(vim.cmd, "tabclose " .. api.nvim_tabpage_get_number(S.tab))
  end
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
end, { nargs = "*", desc = "Open the mksk tab (starts the backend): [plan] [backend args]" })
api.nvim_create_user_command("MkskToggle", function() M.toggle() end,
  { desc = "Focus the mksk chat / back to the previous tab" })
api.nvim_create_user_command("MkskHide", function() M.hide() end, { desc = "Leave the mksk tab" })
api.nvim_create_user_command("MkskQuit", function() M.quit() end, { desc = "Stop mksk, wipe its chat, close its tab" })
api.nvim_create_user_command("MkskAsk", function(o) M.ask(o.args, o) end,
  { nargs = "*", range = true, desc = "Send a message to mksk ([range] attaches lines)" })
api.nvim_create_user_command("MkskDiff", function()
  local ok, out = pcall(P.show_diff)
  if not ok then vim.notify("mksk: " .. tostring(out), vim.log.levels.WARN) end
end, { desc = "Saved | proposed plan, side by side" })
api.nvim_create_user_command("MkskFile", function()
  local ok, out = pcall(open_file, true)
  if not ok then vim.notify("mksk: " .. tostring(out), vim.log.levels.WARN) end
end, { desc = "Focus the plan pane" })
api.nvim_create_user_command("MkskReview", function() M.review() end,
  { desc = "Focus the REVIEW pane (changes reported on the network)" })

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

-- The pane colors follow the colorscheme and 'background' (a colorscheme change clears highlight groups).
local colors_group = api.nvim_create_augroup("mksk_colors", { clear = true })
api.nvim_create_autocmd("ColorScheme", { group = colors_group, callback = function() define_highlights(); refresh() end })
api.nvim_create_autocmd("BufWinEnter", { group = colors_group, callback = function()
  if S and S.wins and S.wins.plan == api.nvim_get_current_win() then refresh() end
end })

-- `:source mksk.lua` opens the UI right away; `require("mksk")` from a config does not.
if MODNAME == nil and not vim.g.mksk_no_autostart then M.start() end

return M
