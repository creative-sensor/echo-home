-- fllm.lua: Neovim frontend for fllm — a frontier model CLI on the Intelligence Network (requires Neovim >= 0.10;
-- 0.11 recommended).
--
-- A frontier model CLI (claude, codex, gemini, aider ... anything that runs in a console) runs in a terminal pane.
-- Its iFace (fllm.py, next to this file) sits in the pane beside it. The iFace has no LLM of its own: it is the
-- frontier's eyes and mouth on echo-net. It logs what is said on the network, types a short notice into the
-- terminal when something new arrives ("[echo-net] 2 new message(s) … read them: fllm_net.py read"), and posts what
-- the frontier sends with fllm_net.py say. On joining it briefs the frontier: it is on a network with other agents.
--
--   ┌───────────────────────────┬──────────────────────────────────────────┐
--   │ IFACE                     │ TERMINAL — the frontier CLI               │
--   │ echo-net traffic, notices │ a real terminal: use the CLI yourself at  │
--   │ typed, what the frontier  │ any time. While you are in terminal mode  │
--   │ read and said             │ here, the iFace never types.              │
--   │ CLAUDE ❯ input line       │                                           │
--   └───────────────────────────┴──────────────────────────────────────────┘
--
-- The screen tap: Neovim's own terminal emulator renders the CLI into the terminal buffer, so any CLI works. This
-- file pushes the visible rows to fllm.py whenever they change (it tells from screen markers when the frontier is
-- idle enough to be told something) and types for it (bracketed paste when the CLI turned it on; cursor keys in
-- the mode the CLI asked for). The CLI gets $FLLM_SESSION: fllm_net.py finds its iFace there.
--
-- Sessions are per profile (g:fllm_clis): :Fllm claude and :Fllm codex each get a tab, a terminal, an iFace and a
-- network identity. Each profile is an agent of the inet driver: :InetJoin claude plugs it into echo-net.
--
--   :Fllm [profile] [cli args]   open the profile's tab (starts the CLI and the iFace; args go to the CLI)
--   :FllmToggle [profile]        go to the IFACE input / back to the previous tab (default key <M-f>, n/i modes)
--   :FllmTerm [profile]          go to the terminal pane in terminal mode
--   :FllmAsk <text>              type text into the frontier of this tab (or the last used) once it is ready
--   :FllmRestart [profile] [args] start the CLI again in the terminal pane (briefed again when on the network)
--   :FllmQuit [profile]          stop the CLI and the iFace, close the tab
--   :InetJoin <profile> / :InetLeave <profile>   on / off the network (profile g:inet_agents.<profile>, optional)
--
-- Options (vimpage_fllm): g:fllm_clis {profile: {cmd, label, purpose, brief, args, busy, ask, ready, allow, nudge,
--   cwd, env}}, g:fllm_default, g:fllm_python, g:fllm_args (backend args), g:fllm_nudge, g:fllm_feed_dir,
--   g:fllm_net_allow, g:fllm_busy/ask/ready, g:fllm_toggle_key, g:fllm_iface_width, g:fllm_poll_ms.
--
-- IFACE pane: <CR> type it into the frontier │ ↑/↓ history │ C-c drop / clear the line │ (normal) <Tab> terminal, q back
-- TERMINAL pane: <C-\><C-n> leaves terminal mode │ (normal) <Tab> IFACE input, q back, i/a terminal mode again

local MODNAME = ...
do -- sourcing again must not orphan running sessions: show the default one instead
  local prev = package.loaded.fllm
  if type(prev) == "table" and prev._loaded then
    if MODNAME == nil and not vim.g.fllm_no_autostart then prev.open() end
    return prev
  end
end

local M = { _loaded = true }
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

--- Git Bash/MSYS hands nvim paths like /c/Users/..; native Windows python needs C:/Users/.. (as in mksk.lua).
local function native(path)
  if fn.has("win32") == 1 and path then
    local drive, rest = path:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(path) == 0 and fn.filereadable(path) == 0 then path = drive:upper() .. ":/" .. rest end
    path = path:gsub("\\", "/")
  end
  return path
end

local SCRIPT_DIR = native(fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h"))
local INET_DIR = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))

local NS = api.nvim_create_namespace("fllm")
local NS_PROMPT = api.nvim_create_namespace("fllm_prompt")
local MAX_LINES = 10000
local LABELS = { claude = "Claude Code", codex = "OpenAI Codex CLI", gemini = "Gemini CLI", aider = "aider",
  opencode = "opencode", qwen = "Qwen Code", ["cursor-agent"] = "Cursor Agent", amp = "Amp", goose = "goose" }
local FRONTIER_HL = { working = "FllmBusy", starting = "FllmBusy", asking = "FllmAsk", ready = "FllmIdle",
  quiet = "FllmIdle", error = "FllmAsk", exited = "FllmAsk", ["not started"] = "FllmTitle" }

local sessions = {}   -- profile key -> session
local counter = 0     -- session numbers (style highlight groups are per backend)
local last_key        -- the session used last
local prev_tab        -- the last tab that is not a fllm tab
local timer           -- polls the terminal screens
local INET            -- the inet driver (false: not loadable)
local registered = {} -- profile keys registered as inet agents

-- ---------------------------------------------------------------- highlights & helpers
local function define_highlights()
  local hl = {
    FllmBadge = { bg = "#d7875f", fg = "#000000", bold = true },
    FllmBadgeInv = { bg = "#000000", fg = "#d7875f", bold = true }, -- the badge's colors inverted: " fllm "
    FllmTitle = { bg = "#303030", fg = "#d0d0d0" },
    FllmIdle = { bg = "#005f00", fg = "#ffffff", bold = true },
    FllmBusy = { bg = "#af5f00", fg = "#ffffff", bold = true },
    FllmAsk = { bg = "#af0000", fg = "#ffffff", bold = true },
    FllmOperator = { bg = "#5f00af", fg = "#ffffff", bold = true },
    FllmHint = { fg = "#808080" },
    FllmUser = { fg = "#5fd7ff", bold = true },
  }
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
end

local function valid_win(w) return w ~= nil and api.nvim_win_is_valid(w) end
local function valid_buf(b) return b ~= nil and api.nvim_buf_is_valid(b) end
local function oneline(s) return (tostring(s or ""):gsub("[\r\n]+", " ")) end
local function notify(msg, level) vim.notify("fllm: " .. msg, level or vim.log.levels.INFO) end
local function tab_valid(S) return S.tab ~= nil and api.nvim_tabpage_is_valid(S.tab) end
local function in_tab(S, w) return valid_win(w) and tab_valid(S) and api.nvim_win_get_tabpage(w) == S.tab end
local function shown(S) return S ~= nil and tab_valid(S) and api.nvim_get_current_tabpage() == S.tab end

local function send(S, msg)
  if S and S.job then pcall(fn.chansend, S.job, vim.json.encode(msg) .. "\n") end
end

local function normalize_args(args)
  if type(args) == "string" then args = vim.split(vim.trim(args), "%s+", { trimempty = true }) end
  local out = {}
  for _, a in ipairs(type(args) == "table" and args or {}) do out[#out + 1] = tostring(a) end
  return out
end

-- ---------------------------------------------------------------- profiles
local function profiles()
  local p = vim.g.fllm_clis
  return type(p) == "table" and p or {}
end

local function default_key()
  if vim.g.fllm_default and vim.g.fllm_default ~= "" then return tostring(vim.g.fllm_default) end
  local keys = vim.tbl_keys(profiles())
  table.sort(keys)
  return keys[1] or "claude"
end

local function profile(key)
  local p = vim.deepcopy(profiles()[key] or {})
  p.key = key
  if p.cmd == nil or p.cmd == "" then p.cmd = key end
  return p
end

--- The command of the terminal: a string runs through 'shell', a list runs as is. extra: args typed after :Fllm.
local function cli_cmd(p, extra)
  extra = extra or {}
  if type(p.cmd) == "table" then
    local cmd = normalize_args(p.cmd)
    return vim.list_extend(cmd, extra)
  end
  local cmd = tostring(p.cmd)
  if #extra > 0 then cmd = cmd .. " " .. table.concat(extra, " ") end
  return cmd
end

local function cmd_text(cmd) return type(cmd) == "table" and table.concat(cmd, " ") or tostring(cmd) end

local function cli_label(p)
  if p.label then return tostring(p.label) end
  local exe = type(p.cmd) == "table" and tostring(p.cmd[1] or p.key) or (tostring(p.cmd):match("^%S+") or p.key)
  exe = fn.fnamemodify(exe, ":t")
  return LABELS[exe] or exe
end

-- ---------------------------------------------------------------- status lines
local function sl(chunks)
  local out = {}
  for _, c in ipairs(chunks) do
    if c[1] ~= "" then out[#out + 1] = "%#" .. c[2] .. "#" .. (c[1]:gsub("%%", "%%%%")) end
  end
  return table.concat(out)
end

local function statusline(S, which)
  local st = S.state
  local left, right = {}, {}
  if which == "iface" then
    left[#left + 1] = { " " .. S.key:upper() .. " · IFACE ", "FllmBadge" }
    left[#left + 1] = { " ● " .. oneline(st.phase or "starting") .. " ", st.item and "FllmBusy" or "FllmIdle" }
    if not S.job then left[#left + 1] = { " backend stopped ", "FllmAsk" } end
    if (st.unread or 0) > 0 then
      left[#left + 1] = { " ✉ " .. st.unread .. " unread" .. (st.nudge and (" · notice in " .. st.nudge .. "s") or "")
        .. " ", "FllmOperator" }
    end
    if (st.queue or 0) > 0 then right[#right + 1] = { " ⌨ " .. st.queue .. " to type ", "FllmTitle" } end
    local net = st.net or {}
    right[#right + 1] = { net.joined and (" 🌐 @" .. oneline(net.name) .. " "
      .. (net.connected and oneline(net.state) or "connecting") .. " ") or " 🌐 off ", "FllmTitle" }
  else
    local f = S.term_job and (st.frontier or "…") or "exited"
    left[#left + 1] = { " " .. S.key:upper() .. " ", "FllmBadge" }
    left[#left + 1] = { " " .. oneline(f) .. " ", FRONTIER_HL[f] or "FllmTitle" }
    left[#left + 1] = { " " .. oneline(S.label) .. " ", "FllmTitle" }
    if st.attention and st.attention ~= "" then right[#right + 1] = { " 🔐 it asks: answer here ", "FllmAsk" } end
    if S.operator then right[#right + 1] = { " ⌨ you type (iFace waits) ", "FllmOperator" } end
    right[#right + 1] = { " <C-\\><C-n> normal │ <Tab> iface ", "FllmHint" }
  end
  return sl(left) .. "%<%*%=" .. sl(right) .. "%*"
end

local function refresh(S)
  if not S then return end
  for _, which in ipairs({ "iface", "term" }) do
    local w = S.wins[which]
    if in_tab(S, w) then pcall(function() vim.wo[w].statusline = statusline(S, which) end) end
  end
end

-- ---------------------------------------------------------------- IFACE prompt line
local function update_prompt(S)
  local tag, badge, arrow = " fllm ", " " .. S.key:upper() .. " ", " ❯ "
  local text = tag .. badge .. arrow
  local ib, old = S.bufs.iface, S.prompt
  if not valid_buf(ib) then return end
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
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_clear_namespace(ib, NS_PROMPT, 0, -1)
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, 0, { end_col = #tag, hl_group = "FllmBadgeInv" })
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, #tag, { end_col = #tag + #badge, hl_group = "FllmBadge" })
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, #tag + #badge, { end_col = #text, hl_group = "FllmHint" })
end

local function input_text(S)
  local ib = S.bufs.iface
  local n = api.nvim_buf_line_count(ib)
  return api.nvim_buf_get_lines(ib, n - 1, n, false)[1]:sub(#S.prompt + 1)
end

local function set_input(S, text)
  local ib = S.bufs.iface
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_set_lines(ib, n - 1, n, false, { S.prompt .. text })
  update_prompt(S)
  local w = S.wins.iface
  if valid_win(w) and api.nvim_win_get_buf(w) == ib then api.nvim_win_set_cursor(w, { n, #S.prompt + #text }) end
end

-- ---------------------------------------------------------------- IFACE output (above the prompt line)
local function hl_name(S, id) return ("FllmS%d_%d"):format(S.n, id) end

local function append(S, segs)
  local buf = S.bufs.iface
  if not valid_buf(buf) then return end
  local lines, hls = { "" }, {}
  for _, seg in ipairs(segs) do
    local parts = vim.split(seg[1], "\n", { plain = true })
    for i, part in ipairs(parts) do
      if i > 1 then lines[#lines + 1] = "" end
      if #part > 0 then
        local s = #lines[#lines]
        lines[#lines] = lines[#lines] .. part
        if seg[2] ~= 0 then hls[#hls + 1] = { #lines, s, s + #part, hl_name(S, seg[2]) } end
      end
    end
  end
  if #lines == 1 and lines[1] == "" then return end
  local closed = lines[#lines] == ""
  if closed then table.remove(lines) end
  local endrow = api.nvim_buf_line_count(buf) - 1 -- rows [0, endrow) are output; the last one is the prompt
  local start_row, first_col
  if S.tail and endrow > 0 then
    local row = endrow - 1
    local cur = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    start_row, first_col = row, #cur
    api.nvim_buf_set_text(buf, row, #cur, row, #cur, lines)
  else
    start_row, first_col = endrow, 0
    api.nvim_buf_set_lines(buf, endrow, endrow, false, lines)
  end
  for _, h in ipairs(hls) do
    local off = h[1] == 1 and first_col or 0
    pcall(api.nvim_buf_set_extmark, buf, NS, start_row + h[1] - 1, h[2] + off, { end_col = h[3] + off, hl_group = h[4] })
  end
  S.tail = not closed
  local total = api.nvim_buf_line_count(buf)
  if total > MAX_LINES then api.nvim_buf_set_lines(buf, 0, total - MAX_LINES, false, {}) end
  for _, win in ipairs(fn.win_findbuf(buf)) do -- keep the view at the bottom when the cursor is on the input line
    if api.nvim_win_get_cursor(win)[1] == api.nvim_buf_line_count(buf) then
      api.nvim_win_call(win, function() vim.cmd("normal! zb") end)
    end
  end
end

local function say_local(S, text) append(S, { { text .. "\n", 0 } }) end

local function clear_iface(S)
  local buf = S.bufs.iface
  local n = api.nvim_buf_line_count(buf)
  api.nvim_buf_set_lines(buf, 0, n - 1, false, {})
  api.nvim_buf_clear_namespace(buf, NS, 0, -1)
  S.tail = false
end

-- ---------------------------------------------------------------- the terminal: modes, keys, screen
--- Track the private modes the CLI sets in its raw output: bracketed paste (2004) and application cursor keys (1).
local function scan_modes(S, data)
  local s = S.raw_tail .. table.concat(data, "\n")
  local function last(on, off, cur)
    local a, b, i = 0, 0, 1
    while true do
      local p = s:find(on, i, true)
      if not p then break end
      a, i = p, p + 1
    end
    i = 1
    while true do
      local p = s:find(off, i, true)
      if not p then break end
      b, i = p, p + 1
    end
    if a == 0 and b == 0 then return cur end
    return a > b
  end
  S.paste = last("\27[?2004h", "\27[?2004l", S.paste)
  S.appkeys = last("\27[?1h", "\27[?1l", S.appkeys)
  S.raw_tail = s:sub(-12)
end

local KEYS = { enter = "\r", cr = "\r", ["return"] = "\r", esc = "\27", escape = "\27", tab = "\t", ["s-tab"] = "\27[Z",
  bs = "\127", backspace = "\127", space = " ", del = "\27[3~", home = "\27[H", ["end"] = "\27[F",
  pageup = "\27[5~", pagedown = "\27[6~" }
local ARROWS = { up = "A", down = "B", right = "C", left = "D" }

local function keycode(S, k)
  local name = k:match("^<(.+)>$")
  if not name then return k end
  local low = name:lower()
  if KEYS[low] then return KEYS[low] end
  if ARROWS[low] then return (S.appkeys and "\27O" or "\27[") .. ARROWS[low] end
  local c = low:match("^c%-(%a)$")
  if c then return string.char(c:byte() - 96) end
  local m = name:match("^[MmAa]%-(.)$")
  if m then return "\27" .. m end
  return k
end

--- The human is at the keyboard of the frontier: terminal mode in its buffer.
local function operator_live(S)
  return valid_buf(S.bufs.term) and api.nvim_get_mode().mode:sub(1, 1) == "t"
    and api.nvim_get_current_buf() == S.bufs.term
end

local function set_operator(S, active)
  if S.operator == active then return end
  S.operator = active
  send(S, { t = "operator", active = active })
  refresh(S)
end

local function poll(S)
  if not S.job then return end
  local buf = S.bufs.term
  if not valid_buf(buf) then return end
  local w = S.wins.term
  if valid_win(w) and api.nvim_win_get_buf(w) == buf then
    S.height, S.width = api.nvim_win_get_height(w), api.nvim_win_get_width(w)
  end
  set_operator(S, operator_live(S))
  local h = S.height or 24
  local total = api.nvim_buf_line_count(buf)
  local rows = api.nvim_buf_get_lines(buf, math.max(0, total - h), total, false)
  local text = table.concat(rows, "\n")
  if text == S.last_text and total == S.last_total then return end
  S.last_text, S.last_total = text, total
  send(S, { t = "screen", rows = rows, total = total, height = h, width = S.width, title = vim.b[buf].term_title })
end

local function ensure_timer()
  if timer then return end
  timer = uv.new_timer()
  local ms = tonumber(vim.g.fllm_poll_ms) or 250
  timer:start(ms, ms, vim.schedule_wrap(function()
    for _, S in pairs(sessions) do pcall(poll, S) end
  end))
end

-- ---------------------------------------------------------------- requests from fllm.py
local OPS = {}

--- Type text and/or keys. Returns the screen before typing: where the answer to this message will start.
function OPS.type(S, p)
  local buf = S.bufs.term
  if not S.term_job or not valid_buf(buf) then error("the frontier CLI is not running", 0) end
  if operator_live(S) then
    set_operator(S, true)
    error("operator", 0)
  end
  local total = api.nvim_buf_line_count(buf)
  local h = S.height or 24
  local start = math.max(0, total - h)
  local anchor, off
  for i, l in ipairs(api.nvim_buf_get_lines(buf, start, total, false)) do
    if vim.trim(l) ~= "" then
      anchor, off = l, i - 1
      break
    end
  end
  local text = type(p.text) == "string" and p.text or ""
  if text ~= "" then
    if text:find("\n") then
      if S.paste then -- the CLI takes it as one paste: newlines stay newlines instead of sending early
        text = "\27[200~" .. (text:gsub("\r?\n", "\r")) .. "\27[201~"
      else
        text = (text:gsub("%s*\r?\n%s*", " "))
      end
    end
    fn.chansend(S.term_job, text)
  end
  for _, k in ipairs(type(p.keys) == "table" and p.keys or {}) do fn.chansend(S.term_job, keycode(S, tostring(k))) end
  return { total = total, height = h, start = start, anchor = anchor, anchor_off = off }
end

local function handle_req(S, msg)
  local res = { t = "res", id = msg.id }
  local op = OPS[msg.op]
  if not op then
    res.ok, res.err = false, "unknown op " .. tostring(msg.op)
  else
    local ok, out = pcall(op, S, msg)
    res.ok = ok
    if ok then res.out = out else res.err = tostring(out) end
  end
  send(S, res)
end

-- ---------------------------------------------------------------- backend messages
local function on_message(S, msg)
  local t = msg.t
  if t == "w" then
    append(S, msg.s)
  elseif t == "style" then
    api.nvim_set_hl(0, hl_name(S, msg.id), {
      fg = msg.fg, bg = msg.bg, bold = msg.bold, italic = msg.italic, underline = msg.underline, reverse = msg.reverse,
    })
  elseif t == "state" then
    S.state = msg
    refresh(S)
  elseif t == "req" then
    vim.schedule(function() if sessions[S.key] == S then handle_req(S, msg) end end)
  elseif t == "clear" then
    clear_iface(S)
  elseif t == "attention" then
    S.state.attention = msg.text
    refresh(S)
    if not shown(S) then
      notify(S.key .. " asks: " .. oneline(msg.text):sub(1, 200) .. "  — answer it in its terminal (:FllmTerm "
        .. S.key .. ")", vim.log.levels.WARN)
    end
  elseif t == "notify" then
    if not shown(S) then notify(S.key .. ": " .. oneline(msg.text):sub(1, 300)) end
  end
end

local function on_stdout(S, data)
  data[1] = S.partial .. data[1]
  S.partial = table.remove(data)
  for _, line in ipairs(data) do
    if line ~= "" then
      local ok, msg = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
      if ok and type(msg) == "table" then on_message(S, msg) end
    end
  end
end

-- ---------------------------------------------------------------- layout
local function setup_iface_win(w)
  local wo = vim.wo[w]
  wo.wrap, wo.linebreak, wo.number, wo.relativenumber = true, true, false, false
  wo.signcolumn, wo.foldcolumn, wo.cursorline, wo.list, wo.spell = "no", "0", false, false, false
  wo.winfixwidth = true
  pcall(function() wo.winfixbuf = true end)
end

local function setup_term_win(w)
  local wo = vim.wo[w]
  wo.number, wo.relativenumber, wo.signcolumn, wo.foldcolumn = false, false, "no", "0"
  wo.wrap, wo.list, wo.spell, wo.cursorline = false, false, false, false
  wo.scrolloff, wo.sidescrolloff = 0, 0
end

--- Lay out the session tab (opening it when needed): IFACE left, terminal right. Recreates closed panes.
local function build(S)
  local W = S.wins
  if not tab_valid(S) then
    vim.cmd("tabnew")
    S.tab = api.nvim_get_current_tabpage()
    S.scratch = api.nvim_get_current_buf()
    W.term, W.iface = api.nvim_get_current_win(), nil
    if valid_buf(S.bufs.term) then api.nvim_win_set_buf(W.term, S.bufs.term) end
  elseif api.nvim_get_current_tabpage() ~= S.tab then
    api.nvim_set_current_tabpage(S.tab)
  end
  if not in_tab(S, W.term) then
    local b = S.bufs.term
    if not valid_buf(b) then -- a placeholder until start_cli puts the terminal there
      b = api.nvim_create_buf(false, true)
      vim.bo[b].bufhidden = "wipe"
    end
    if in_tab(S, W.iface) then
      W.term = api.nvim_open_win(b, false, { split = "right", win = W.iface })
    else
      W.term = api.nvim_open_win(b, false, { split = "below", win = -1 })
    end
  end
  setup_term_win(W.term)
  if not in_tab(S, W.iface) then
    local frac = tonumber(vim.g.fllm_iface_width) or 0.4
    W.iface = api.nvim_open_win(S.bufs.iface, false,
      { split = "left", win = W.term, width = math.max(30, math.floor(vim.o.columns * frac)) })
    setup_iface_win(W.iface)
    local n = api.nvim_buf_line_count(S.bufs.iface)
    api.nvim_win_set_cursor(W.iface, { n, 0 })
  end
  refresh(S)
end

local function enter_input(S)
  local w = S.wins.iface
  if not shown(S) or not in_tab(S, w) then return end
  api.nvim_set_current_win(w)
  local n = api.nvim_buf_line_count(S.bufs.iface)
  api.nvim_win_set_cursor(w, { n, #api.nvim_buf_get_lines(S.bufs.iface, n - 1, n, false)[1] })
  vim.cmd("startinsert!")
end

local function enter_term(S)
  local w = S.wins.term
  if not shown(S) or not in_tab(S, w) or not valid_buf(S.bufs.term) then return end
  api.nvim_set_current_win(w)
  if vim.bo[S.bufs.term].buftype == "terminal" then vim.cmd("startinsert") end
end

local function hide(S)
  if not shown(S) then return end
  vim.cmd("stopinsert")
  if prev_tab and prev_tab ~= S.tab and api.nvim_tabpage_is_valid(prev_tab) then
    api.nvim_set_current_tabpage(prev_tab)
  elseif #api.nvim_list_tabpages() > 1 then
    vim.cmd("tabprevious")
  else
    notify("no other tab to go back to")
  end
end

-- ---------------------------------------------------------------- the frontier CLI
local function term_keymaps(S, buf)
  local o = { buffer = buf, nowait = true, silent = true }
  vim.keymap.set("n", "<Tab>", function() enter_input(S) end, o)
  vim.keymap.set("n", "q", function() hide(S) end, o)
end

local function on_cli_exit(S, buf, code)
  if S.bufs.term ~= buf then return end -- a terminal replaced by :FllmRestart
  S.term_job = nil
  send(S, { t = "term", op = "exit", code = code })
  refresh(S)
end

--- Start the CLI in the terminal pane (replacing the old terminal, if any).
local function start_cli(S, extra)
  if not in_tab(S, S.wins.term) then build(S) end
  local W = S.wins
  local old = S.bufs.term
  local buf = api.nvim_create_buf(false, false)
  api.nvim_win_set_buf(W.term, buf)
  S.bufs.term = buf
  S.paste, S.appkeys, S.raw_tail, S.last_text = false, false, "", nil
  local cmd = cli_cmd(S.profile, extra)
  -- the frontier's shell tool inherits these: fllm_net.py finds its iFace through FLLM_SESSION
  local env = vim.tbl_extend("force", type(S.profile.env) == "table" and S.profile.env or {},
    { FLLM_SESSION = S.session, FLLM_NAME = S.key })
  local ok, chan = pcall(api.nvim_win_call, W.term, function()
    local opts = { cwd = S.cwd, env = env,
      on_stdout = function(_, data) scan_modes(S, data) end,
      on_exit = function(_, code) vim.schedule(function() on_cli_exit(S, buf, code) end) end }
    if fn.has("nvim-0.11") == 1 then
      opts.term = true
      return fn.jobstart(cmd, opts)
    end
    return fn.termopen(cmd, opts)
  end)
  if not ok or type(chan) ~= "number" or chan <= 0 then
    say_local(S, "[cannot start " .. cmd_text(cmd) .. ": " .. tostring(chan) .. "]")
    S.term_job = nil
    return false
  end
  S.term_job = chan
  pcall(function() vim.bo[buf].scrollback = 100000 end)
  vim.bo[buf].bufhidden = "hide"
  term_keymaps(S, buf)
  api.nvim_win_call(W.term, function() vim.cmd("normal! G") end)
  if valid_buf(old) and old ~= buf then pcall(api.nvim_buf_delete, old, { force = true }) end
  send(S, { t = "term", op = "start", cmd = cmd_text(cmd) })
  refresh(S)
  return true
end

-- ---------------------------------------------------------------- the backend
local function on_backend_exit(S, job, code)
  if S.job ~= job then return end
  S.job = nil
  say_local(S, "[fllm backend exited with code " .. code .. " — :Fllm " .. S.key .. " starts it again]")
  refresh(S)
end

local function start_backend(S, net_args)
  local p = S.profile
  local py = vim.g.fllm_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", SCRIPT_DIR .. "/fllm.py", "--name", S.key, "--cli", S.label, "--session", S.session,
    "--feed-dir", native(fn.expand(vim.g.fllm_feed_dir or "~/echo-home/Documents/logixa/fllm")) }
  if p.purpose and p.purpose ~= "" then vim.list_extend(cmd, { "--net-purpose", tostring(p.purpose) }) end
  local nudge = p.nudge or vim.g.fllm_nudge
  if nudge and nudge ~= "" then vim.list_extend(cmd, { "--nudge", tostring(nudge) }) end
  local brief = p.brief or vim.g.fllm_brief
  if type(brief) == "table" then brief = table.concat(normalize_args(brief), "\n") end
  if brief and brief ~= "" then vim.list_extend(cmd, { "--brief", tostring(brief) }) end
  vim.list_extend(cmd, normalize_args(vim.g.fllm_args))
  vim.list_extend(cmd, normalize_args(p.args))
  for _, kind in ipairs({ "busy", "ask", "ready" }) do -- screen markers: a profile's own replace the defaults
    local pats = p[kind] or vim.g["fllm_" .. kind]
    if type(pats) == "string" then pats = { pats } end
    for _, b in ipairs(type(pats) == "table" and pats or {}) do vim.list_extend(cmd, { "--" .. kind, tostring(b) }) end
  end
  local allow = p.allow or vim.g.fllm_net_allow
  if type(allow) == "table" then allow = table.concat(normalize_args(allow), ",") end
  if allow and allow ~= "" then vim.list_extend(cmd, { "--net-allow", tostring(allow) }) end
  vim.list_extend(cmd, net_args or {})
  S.partial = ""
  local job
  job = fn.jobstart(cmd, {
    cwd = S.cwd,
    env = { INET_DIR = INET_DIR, PYTHONIOENCODING = "utf-8" },
    on_stdout = function(_, data) if S.job == job then on_stdout(S, data) end end,
    on_stderr = function(_, data)
      local text = table.concat(data, "\n")
      if S.job == job and text:match("%S") then say_local(S, (text:gsub("%s+$", ""))) end
    end,
    on_exit = function(_, code) on_backend_exit(S, job, code) end,
  })
  if job <= 0 then
    notify("failed to start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
    return false
  end
  S.job = job
  S.last_text = nil -- push the screen again
  if S.term_job then send(S, { t = "term", op = "attach", cmd = cmd_text(S.cli) }) end -- a new iFace, the same CLI
  if S.operator then send(S, { t = "operator", active = true }) end
  return true
end

-- ---------------------------------------------------------------- IFACE input
local function mark_user_line(S, row)
  local ib, plen = S.bufs.iface, #S.prompt
  local line = api.nvim_buf_get_lines(ib, row, row + 1, false)[1] or ""
  pcall(api.nvim_buf_set_extmark, ib, NS, row, 0, { end_col = math.min(plen, #line), hl_group = "FllmHint" })
  pcall(api.nvim_buf_set_extmark, ib, NS, row, math.min(plen, #line), { end_col = #line, hl_group = "FllmUser" })
end

local function submit(S, text)
  S.hidx = nil
  if text ~= "" and S.history[#S.history] ~= text then S.history[#S.history + 1] = text end
  if S.job then
    send(S, { t = "input", text = text })
  else
    say_local(S, "backend is not running; :Fllm " .. S.key .. " starts it again")
  end
end

local function on_submit(S, text)
  -- the prompt buffer already turned the input into a transcript line (row n-2) and added a new prompt
  local n = api.nvim_buf_line_count(S.bufs.iface)
  mark_user_line(S, n - 2)
  S.tail = false
  submit(S, text)
  vim.schedule(function() if sessions[S.key] == S then update_prompt(S) end end)
end

local function history_step(S, delta)
  local h = S.history
  if #h == 0 then return end
  if not S.hidx then
    S.hdraft = input_text(S)
    S.hidx = #h + 1
  end
  S.hidx = math.max(1, math.min(#h + 1, S.hidx + delta))
  set_input(S, S.hidx > #h and S.hdraft or oneline(h[S.hidx]))
end

local function create_iface_buf(S)
  local b = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, b, "fllm://" .. S.key .. "/iface")
  vim.bo[b].buftype, vim.bo[b].bufhidden, vim.bo[b].swapfile = "prompt", "hide", false
  vim.bo[b].filetype = "fllmiface"
  fn.prompt_setcallback(b, function(text) on_submit(S, text) end)
  fn.prompt_setinterrupt(b, function()
    if S.state.job then send(S, { t = "cancel" }) else set_input(S, "") end
  end)
  local o = { buffer = b, nowait = true, silent = true }
  vim.keymap.set("i", "<Up>", function() history_step(S, -1) end, o)
  vim.keymap.set("i", "<Down>", function() history_step(S, 1) end, o)
  vim.keymap.set({ "n", "i" }, "<C-c>", function()
    if S.state.job then send(S, { t = "cancel" }) else set_input(S, "") end
  end, o)
  vim.keymap.set("n", "<Tab>", function() enter_term(S) end, o)
  vim.keymap.set("n", "q", function() hide(S) end, o)
  for _, lhs in ipairs({ "i", "a", "I", "A", "o", "<CR>" }) do
    vim.keymap.set("n", lhs, function() enter_input(S) end, o)
  end
  return b
end

-- ---------------------------------------------------------------- Intelligence Network
local function load_inet()
  if INET ~= nil then return INET end
  local ok, inet = pcall(function() return package.loaded.inet or dofile(INET_DIR .. "/inet.lua") end)
  INET = (ok and type(inet) == "table") and inet or false
  if not ok then notify("Intelligence Network driver not loaded (" .. INET_DIR .. "/inet.lua): " .. tostring(inet),
      vim.log.levels.WARN) end
  return INET
end

--- Each profile is an agent of the inet driver: :InetJoin <profile> starts it already plugged in.
local function register(key)
  local inet = load_inet()
  if not inet or registered[key] then return end
  registered[key] = true
  inet.register_agent(key, {
    join = function(p)
      if p.purpose == "" and profile(key).purpose then p.purpose = tostring(profile(key).purpose) end
      local S = sessions[key]
      if S and S.job then
        send(S, { t = "net", op = "join", name = p.name, purpose = p.purpose ~= "" and p.purpose or nil,
          hub = p.hub, network = p.network.name })
      else
        M.open(key, nil, inet.agent_args(p))
      end
    end,
    leave = function()
      local S = sessions[key]
      if S and S.job then send(S, { t = "net", op = "leave" }) end
    end,
    status = function()
      local S = sessions[key]
      return S and S.job and S.state.net or nil
    end,
  })
end

-- ---------------------------------------------------------------- sessions
local function find(key)
  if key and key ~= "" then return sessions[key] end
  local tab = api.nvim_get_current_tabpage()
  for _, S in pairs(sessions) do
    if S.tab == tab then return S end
  end
  if last_key and sessions[last_key] then return sessions[last_key] end
  local _, S = next(sessions)
  return S
end

local function new_session(key)
  define_highlights()
  counter = counter + 1
  local p = profile(key)
  local S = { key = key, n = counter, profile = p, label = cli_label(p), bufs = {}, wins = {}, history = {},
    state = { phase = "starting" }, prompt = "", partial = "", tail = false, operator = false,
    paste = false, appkeys = false, raw_tail = "",
    cwd = native(fn.fnamemodify(fn.expand(p.cwd or fn.getcwd()), ":p")):gsub("[/\\]$", "") }
  -- where fllm_net.py (run by the frontier) finds the address and token of this session's iFace
  S.session = native(fn.stdpath("state") .. "/fllm/" .. key:gsub("[^%w%-_.]", "_") .. ".json")
  S.bufs.iface = create_iface_buf(S)
  sessions[key] = S
  update_prompt(S)
  register(key)
  ensure_timer()
  return S
end

local function show(S, enter)
  last_key = S.key
  build(S)
  if enter == "term" then enter_term(S) elseif enter ~= false then enter_input(S) end
end

--- Open a profile's tab, starting what is not running: the CLI (extra: its args) and the iFace (net_args: joined).
function M.open(key, extra, net_args)
  key = (key and key ~= "") and key or (last_key and sessions[last_key] and last_key) or default_key()
  local S = sessions[key] or new_session(key)
  last_key = key
  if not S.job and not start_backend(S, net_args) then return end
  build(S)
  if not S.term_job then
    S.cli = cli_cmd(S.profile, extra)
    start_cli(S, extra)
  elseif extra and #extra > 0 then
    notify(key .. " runs already; :FllmRestart " .. key .. " " .. table.concat(extra, " ") .. " starts it with these args")
  end
  if valid_buf(S.scratch) and S.scratch ~= S.bufs.term and api.nvim_buf_get_name(S.scratch) == ""
    and not vim.bo[S.scratch].modified then
    pcall(api.nvim_buf_delete, S.scratch, {})
  end
  S.scratch = nil
  show(S, true)
end

--- IFACE input ⇄ previous tab. Never starts a CLI by itself (a frontier CLI may cost money): :Fllm does.
function M.toggle(key)
  local here = find(nil)
  if here and shown(here) and (not key or key == here.key) then return hide(here) end
  local S = find(key)
  if S then return show(S, true) end
  notify("no session" .. (key and (" '" .. key .. "'") or "") .. " running — :Fllm " .. (key or default_key())
    .. " starts one")
end

function M.term(key)
  local S = find(key)
  if not S then return M.open(key) end
  show(S, "term")
end

function M.ask(text, key)
  text = vim.trim(text or "")
  if text == "" then return notify("usage: :FllmAsk <text>", vim.log.levels.WARN) end
  local S = find(key)
  if not S or not S.job then
    M.open(key)
    S = find(key)
  end
  if not S or not S.job then return end
  append(S, { { S.prompt .. text .. "\n", 0 } })
  mark_user_line(S, api.nvim_buf_line_count(S.bufs.iface) - 2)
  submit(S, text)
end

function M.restart(key, extra)
  local S = find(key)
  if not S then return M.open(key, extra) end
  if S.term_job then pcall(fn.jobstop, S.term_job) end
  S.term_job = nil
  show(S, false)
  S.cli = cli_cmd(S.profile, extra)
  start_cli(S, extra)
  enter_term(S)
end

function M.quit(key)
  local S = find(key)
  if not S then return notify("no fllm session" .. (key and (" '" .. key .. "'") or "")) end
  sessions[S.key] = nil
  if last_key == S.key then last_key = nil end
  if S.job then
    send(S, { t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
    S.job = nil
  end
  if S.term_job then pcall(fn.jobstop, S.term_job) end
  if tab_valid(S) and #api.nvim_list_tabpages() > 1 then
    if shown(S) then vim.cmd("stopinsert") end
    pcall(vim.cmd, "tabclose " .. api.nvim_tabpage_get_number(S.tab))
  end
  for _, b in pairs(S.bufs) do
    if valid_buf(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  if next(sessions) == nil and timer then
    timer:stop()
    timer:close()
    timer = nil
  end
end

function M.list()
  local out = {}
  for key, S in pairs(sessions) do
    local net = S.state.net or {}
    out[#out + 1] = ("%s: %s · frontier %s · iFace %s%s"):format(key, cmd_text(S.cli or S.profile.cmd),
      S.term_job and (S.state.frontier or "?") or "exited", S.job and (S.state.phase or "?") or "stopped",
      net.joined and (" · @" .. tostring(net.name) .. " on " .. tostring(net.network)) or "")
  end
  return out
end

function M.sessions() return sessions end

-- ---------------------------------------------------------------- commands, keys, autocmds
local function complete_profiles()
  local seen, out = {}, {}
  for k in pairs(profiles()) do seen[k] = true end
  for k in pairs(sessions) do seen[k] = true end
  for k in pairs(seen) do out[#out + 1] = k end
  table.sort(out)
  return out
end

local cmd = api.nvim_create_user_command
cmd("Fllm", function(o)
  local args = vim.deepcopy(o.fargs)
  local key = (args[1] and args[1]:sub(1, 1) ~= "-") and table.remove(args, 1) or nil
  M.open(key, args)
end, { nargs = "*", complete = complete_profiles, desc = "Open a frontier CLI tab with its iFace: [profile] [cli args]" })
cmd("FllmToggle", function(o) M.toggle(o.args ~= "" and o.args or nil) end,
  { nargs = "?", complete = complete_profiles, desc = "fllm IFACE input / back to the previous tab" })
cmd("FllmTerm", function(o) M.term(o.args ~= "" and o.args or nil) end,
  { nargs = "?", complete = complete_profiles, desc = "Go to the frontier terminal (terminal mode)" })
cmd("FllmAsk", function(o) M.ask(o.args) end, { nargs = "+", desc = "Type text into the frontier through its iFace, once it is ready" })
cmd("FllmRestart", function(o)
  local args = vim.deepcopy(o.fargs)
  local key = (args[1] and sessions[args[1]]) and table.remove(args, 1) or nil
  M.restart(key, args)
end, { nargs = "*", complete = complete_profiles, desc = "Start the frontier CLI again: [profile] [cli args]" })
cmd("FllmQuit", function(o) M.quit(o.args ~= "" and o.args or nil) end,
  { nargs = "?", complete = complete_profiles, desc = "Stop a frontier CLI and its iFace, close its tab" })
cmd("FllmStatus", function()
  local l = M.list()
  notify(#l > 0 and table.concat(l, "\n") or "no session (profiles: " .. table.concat(complete_profiles(), ", ") .. ")")
end, { desc = "fllm sessions" })

local key = vim.g.fllm_toggle_key
if key == nil then key = "<M-f>" end
if key ~= "" then vim.keymap.set({ "n", "i" }, key, function() M.toggle() end, { silent = true, desc = "fllm toggle" }) end

local group = api.nvim_create_augroup("Fllm", { clear = true })
api.nvim_create_autocmd("TabLeave", {
  group = group,
  callback = function()
    local t = api.nvim_get_current_tabpage()
    for _, S in pairs(sessions) do
      if S.tab == t then return end
    end
    prev_tab = t
  end,
})
api.nvim_create_autocmd({ "TermEnter", "TermLeave" }, {
  group = group,
  callback = function(ev)
    for _, S in pairs(sessions) do
      if valid_buf(S.bufs.term) then set_operator(S, operator_live(S) and ev.event == "TermEnter") end
    end
  end,
})
api.nvim_create_autocmd("WinClosed", {
  group = group,
  callback = function(ev)
    local id = tonumber(ev.match)
    for _, S in pairs(sessions) do
      for k, w in pairs(S.wins) do
        if w == id then S.wins[k] = nil end
      end
    end
  end,
})
api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function()
    for _, S in pairs(sessions) do
      send(S, { t = "quit" })
      if S.term_job then pcall(fn.jobstop, S.term_job) end
    end
  end,
})

package.loaded.fllm = M
for k in pairs(profiles()) do register(k) end

-- `:source fllm.lua` opens the default profile right away; `require("fllm")` from a config does not.
if MODNAME == nil and not vim.g.fllm_no_autostart then M.open() end

return M
