-- ptrm.lua: Neovim frontend of ptrm, the port-remote agent (requires Neovim >= 0.10).
-- Loaded only through ~/echo-home/.vim/mimd/vimpage_ptrm.
--
-- The agent (ptrm.py, same folder; engine ptrm_core.py) puts services that run on remote, high-performance hosts at
-- ports of this machine and keeps them there: the rows of your ptrm.md (localhost:<LOCAL_PORT> -> <REMOTE_IP>:
-- <REMOTE_PORT> through <SSH_HOST>), ssh tunnels it opens and heals, what answers behind them, what runs on the hosts
-- (discovery), launching a missing service from a remembered recipe, troubleshooting — and it remembers every port
-- ever opened and every host ever accessed (~/echo-home/memory/ptrm).
--
-- ptrm has its own tab page:
--     ┌─ DASH  ✅ 1 up · 🟠 1 down · 6 off ──────────┬─ STATS macmon · local ─┬─ STATS htop · serpenti ─┐
--     │ the table, live: port · state · host ·        │ a monitor terminal     │ another one (a host's   │  top: the
--     │ remote · service (▸ model) · tunnel · ms      ├─ STATS nvtop · hostB ──┤ htop, btop, nvtop …)    │  connections
--     │ HOSTS (what ptrm knows of each) · RECENT      │                        │                         │  and the hosts
--     ├─ PTRM  ○ idle ─────────────────────┬─────────┴── LLM  raw output ───┬┴─ IFACE ────────────────┤
--     │ what ptrm does, your input on the  │ the raw LLM output             │ the network (while      │  bottom: PTRM ·
--     │ last line                          │                                │ joined)                 │  LLM · IFACE
--     └────────────────────────────────────┴────────────────────────────────┴─────────────────────────┘
-- A shell opened by t (TERM) splits DASH: it goes below it, in DASH's column.
-- STATS is a grid of terminals (g:ptrm_stats_rows per column), each a live monitor on a host (ssh -t) or this machine;
-- each terminal's buffer is named <user>@<host>. The LLM pane streams the model's thinking (💭, PtrmThink) before
-- each answer (--no-think, or ===T, turns it off).
--
-- `:source ptrm.lua` (ptrm1) starts ptrm and opens its tab. Sourced again while ptrm runs, it only goes to the tab;
-- once ptrm is stopped (:PtrmQuit), sourcing reloads this file fresh.
--
--   :Ptrm [args]         start (backend args from g:ptrm_args) / go to the ptrm tab / back to your tab
--   :PtrmToggle          the same (vimpage_ptrm maps it to ptrmk)
--   :PtrmAsk <text>      a request without going to the tab
--   :PtrmCheck [port]    check a port (the row under the cursor in ptrm.md, else ask) · :PtrmOpen · :PtrmClose ·
--   :PtrmReopen          the same for opening / closing / reopening its tunnel (no LLM)
--   :PtrmBringup [port]  bring the row up (choose a host, open, launch what NOTES recalls, else teach)
--   :PtrmTeach [port]    open a terminal on the row's host in STATS to show ptrm how to start the service
--   :PtrmDisable [port]  disable the row (LOCAL_PORT written ~~port~~): ptrm leaves it alone · :PtrmEnable [port]
--   :PtrmDisableHost [host]   ptrm stops using the host (Xh in DASH) · :PtrmEnableHost [host]
--   :PtrmStats [host] [tool]   a monitor terminal in STATS (no host: this machine; tool: top htop btop nvtop macmon gpu)
--   :PtrmTerm [host]     a shell on the host in STATS to type commands there (the row under the cursor; 'local')
--   :PtrmTable           edit the table (ptrm.md)
--   :PtrmCancel          cancel the running task       :PtrmQuit  stop the backend (the tunnels stay open)
--   :InetJoin ptrm / :InetLeave ptrm    (profile g:inet_agents.ptrm in vimpage_inet, optional)
--
-- Options: g:ptrm_python ("python"), g:ptrm_args (list), g:ptrm_table (ptrm.md; default as VimPage_ptrm:
--          <pages>/<g:hostname>.ptrm.md, else ptrm.md), g:ptrm_memory (~/echo-home/memory/ptrm: the base; this
--          machine writes <base>/<hostname>/ and reads the other clients' folders),
--          g:ptrm_stats ({'hosts'}: the monitors opened at start — 'hosts': one on each host of the table once ptrm
--          reaches it; 'local', 'local:btop', 'user@host', 'user@host:nvtop'),
--          g:ptrm_stats_rows (2: panes per STATS column), g:ptrm_stats_max (6), g:ptrm_dash_width (0.5: DASH's share
--          of the top row), g:ptrm_term_height (0.4: the shells of t below DASH, a fraction of the top row or rows),
--          g:ptrm_bottom_height (rows, or a fraction of the screen; default 38% less 2 lines), g:ptrm_llm_width (0.34),
--          g:ptrm_iface_width (0.26): fractions of the width, g:ptrm_toggle_key (e.g. '<M-p>'), g:ptrm_no_autostart.
-- In the tab: last line of PTRM is the input · <CR> send · <C-c> cancel task · <Tab> (normal) next pane ·
--             <S-Tab> previous pane · i input · q back to your tab (normal)
-- In DASH:   <CR> check the port (a host line: reach it) · o open · x close · R reopen · B bring up · T teach ·
--            D diagnose · X disable / enable the row (a HOSTS line: the host) · Xh disable / enable the line's host ·
--            s stats of the row's host · t a terminal (shell) on it
--            (header ' TERM ' ' <username> ' ' <hostname> ': PtrmTermBadge / PtrmTermUser / PtrmTermHost) ·
--            S stats of this machine · d discover the host · h history · a ask about the row · e edit the table ·
--            r refresh
-- In STATS:  x close the pane · R restart it · i / a type into the monitor (terminal mode; <C-\><C-n> back)

local MODNAME = ...

do -- sourced again: a running ptrm keeps its session (only its tab is shown); a stopped one gives way to this load
  local old = package.loaded.ptrm
  if MODNAME == nil and type(old) == "table" then
    local alive = old.running
    if type(alive) == "function" and alive() then
      old.show()
      return old
    end
    if type(old.quit) == "function" then pcall(old.quit) end
  end
end

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
local NS = api.nvim_create_namespace("ptrm")
local NS_DASH = api.nvim_create_namespace("ptrm_dash")
local NS_TABLE = api.nvim_create_namespace("ptrm_table")
-- The input prompt " PorTReMote ": " PorT" as the PTRM header badge, "ReMote " on the header's strip.
local PROMPT_PARTS = { { " PorT", "PtrmPrompt" }, { "ReMote ", "PtrmPromptInv" } }
local PROMPT = PROMPT_PARTS[1][1] .. PROMPT_PARTS[2][1]
local BOTTOM = { "chat", "log", "iface" }
local BADGE = { chat = "PTRM", log = "LLM", iface = "IFACE" }
local COLOR = { chat = "Main", log = "Llm", iface = "Iface" } -- Ptrm<COLOR>Bar / Ptrm<COLOR>Badge
local S -- session; nil when not running

-- Pane headers: a badge and a strip per pane. PTRM in gruvbox's green (links up), DASH in its aqua, STATS in its
-- yellow, LLM in its blue (as in hbmx/knbg/disp), IFACE in its purple.
-- Override with :hi PtrmPrompt … / PtrmMainBar / PtrmMainBadge / PtrmDashBadge / PtrmStatsBar … (defaults, re-applied
-- after a colorscheme change).
local function set_hl()
  local d = function(name, spec) spec.default = true; api.nvim_set_hl(0, name, spec) end
  local light = vim.o.background == "light"
  local P = light and {
    Main = { bar = "#e6e9c0", fg = "#3c3a0a", badge = "#79740e", on = "#fbf1c7", cbar = 230, cfg = 58, cb = 100, con = 230 },
    Llm = { bar = "#cadbea", badge = "#076678", on = "#fbf1c7", cbar = 153, cb = 24, con = 230 },
    Iface = { bar = "#e7cfdc", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cb = 96, con = 230 },
    Dash = { bar = "#d5e6d3", fg = "#1d3b2a", badge = "#427b58", on = "#fbf1c7", cbar = 194, cfg = 22, cb = 29, con = 230 },
    Stats = { bar = "#f2e5bc", fg = "#5a4209", badge = "#b57614", on = "#fbf1c7", cbar = 230, cfg = 94, cb = 136, con = 230 },
    StatsOff = { bar = "#ebdbb2", fg = "#7c6f64", badge = "#928374", on = "#fbf1c7", cbar = 223, cfg = 243, cb = 245, con = 230 },
  } or {
    Main = { bar = "#32361a", fg = "#e0e8b0", badge = "#b8bb26", on = "#1d2021", cbar = 58, cfg = 187, cb = 142, con = 234 },
    Llm = { bar = "#2a3b48", badge = "#83a598", on = "#1d2021", cbar = 17, cb = 109, con = 234 },
    Iface = { bar = "#43304a", badge = "#d3869b", on = "#1d2021", cbar = 53, cb = 175, con = 234 },
    Dash = { bar = "#263b30", fg = "#cfe8d0", badge = "#8ec07c", on = "#1d2021", cbar = 22, cfg = 194, cb = 108, con = 234 },
    Stats = { bar = "#3c3418", fg = "#f2e5bc", badge = "#fabd2f", on = "#1d2021", cbar = 58, cfg = 230, cb = 214, con = 234 },
    StatsOff = { bar = "#32302f", fg = "#a89984", badge = "#7c6f64", on = "#1d2021", cbar = 236, cfg = 246, cb = 243, con = 234 },
  }
  local b = P.Main
  d("PtrmPrompt", { bg = b.badge, fg = b.on, bold = true, ctermbg = b.cb, ctermfg = b.con, cterm = { bold = true } })
  d("PtrmPromptInv", { bg = b.bar, fg = b.badge, bold = true, ctermbg = b.cbar, ctermfg = b.cb, cterm = { bold = true } })
  for name, p in pairs(P) do
    d("Ptrm" .. name .. "Bar", { bg = p.bar, fg = p.fg, ctermbg = p.cbar, ctermfg = p.cfg })
    d("Ptrm" .. name .. "Badge", { bg = p.badge, fg = p.on, bold = true, ctermbg = p.cb, ctermfg = p.con, cterm = { bold = true } })
  end
  d("PtrmDashSel", { link = "CursorLine" })
  -- DASH's HOSTS lines: the host name and the labels ' OS ' ' CPU ' ' MEM ' ' GPU ' in inverted colours (dark chips
  -- on a light background, light ones on a dark one), each value right after its label in the opposite colours.
  -- PtrmInvNameBad: the host did not answer last time.
  if light then
    d("PtrmInvName", { bg = "#073642", fg = "#fdf6e3", bold = true, ctermbg = 23, ctermfg = 230, cterm = { bold = true } })
    d("PtrmInvNameBad", { bg = "#6c1d1d", fg = "#fdf6e3", bold = true, ctermbg = 52, ctermfg = 230, cterm = { bold = true } })
    d("PtrmInvLabel", { bg = "#073642", fg = "#eee8d5", bold = true, ctermbg = 23, ctermfg = 254, cterm = { bold = true } })
    d("PtrmInvVal", { bg = "#eee8d5", fg = "#073642", bold = true, ctermbg = 254, ctermfg = 23, cterm = { bold = true } })
  else
    d("PtrmInvName", { bg = "#ebdbb2", fg = "#1d2021", bold = true, ctermbg = 223, ctermfg = 234, cterm = { bold = true } })
    d("PtrmInvNameBad", { bg = "#fb4934", fg = "#1d2021", bold = true, ctermbg = 167, ctermfg = 234, cterm = { bold = true } })
    d("PtrmInvLabel", { bg = "#ebdbb2", fg = "#1d2021", bold = true, ctermbg = 223, ctermfg = 234, cterm = { bold = true } })
    d("PtrmInvVal", { bg = "#3c3836", fg = "#fabd2f", bold = true, ctermbg = 237, ctermfg = 214, cterm = { bold = true } })
  end
  -- A host the user disabled (Xh): its name greyed and struck through.
  d("PtrmInvNameOff", light and { bg = "#93a1a1", fg = "#eee8d5", bold = true, strikethrough = true, ctermbg = 247, ctermfg = 254,
    cterm = { bold = true, strikethrough = true } }
    or { bg = "#504945", fg = "#a89984", bold = true, strikethrough = true, ctermbg = 239, ctermfg = 246,
      cterm = { bold = true, strikethrough = true } })
  -- ' NET ' values, each in its own colour (medium · network name · IP), and the ' SVC ' count: inverted chips.
  d("PtrmNetType", { bg = "#b58900", fg = "#002b36", bold = true, ctermbg = 136, ctermfg = 234, cterm = { bold = true } })
  d("PtrmNetName", { bg = "#2aa198", fg = "#002b36", bold = true, ctermbg = 37, ctermfg = 234, cterm = { bold = true } })
  d("PtrmNetIP", { bg = "#6c71c4", fg = "#fdf6e3", bold = true, ctermbg = 61, ctermfg = 230, cterm = { bold = true } })
  d("PtrmSvcVal", { bg = "#859900", fg = "#002b36", bold = true, ctermbg = 100, ctermfg = 234, cterm = { bold = true } })
  -- PORT up: dark green bold · REMOTE_PORT not LOCAL_PORT: dark orange bold · a disabled row: struck through (DASH and
  -- the table's buffer).
  d("PtrmPortUp", light and { fg = "#1b6e20", bold = true, ctermfg = 22, cterm = { bold = true } }
    or { fg = "#3fa34d", bold = true, ctermfg = 71, cterm = { bold = true } })
  d("PtrmPortDiff", light and { fg = "#c14d00", bold = true, ctermfg = 166, cterm = { bold = true } }
    or { fg = "#e07b24", bold = true, ctermfg = 208, cterm = { bold = true } })
  d("PtrmPortOff", { fg = light and "#93a1a1" or "#7c6f64", strikethrough = true, ctermfg = 245, cterm = { strikethrough = true } })
  d("PtrmDisabled", { link = "Comment" })
  -- The header of a terminal opened by t: ' TERM ' and ' <username> ' inverted against each other, then
  -- ' <hostname> ' in a colour of its own across the rest of the pane's width.
  if light then
    d("PtrmTermBadge", { bg = "#3c1361", fg = "#f3e8ff", bold = true, ctermbg = 54, ctermfg = 225, cterm = { bold = true } })
    d("PtrmTermUser", { bg = "#f3e8ff", fg = "#3c1361", bold = true, ctermbg = 225, ctermfg = 54, cterm = { bold = true } })
    d("PtrmTermHost", { bg = "#0f5e6e", fg = "#e6fffb", bold = true, ctermbg = 24, ctermfg = 195, cterm = { bold = true } })
  else
    d("PtrmTermBadge", { bg = "#c4a7e7", fg = "#1d2021", bold = true, ctermbg = 183, ctermfg = 234, cterm = { bold = true } })
    d("PtrmTermUser", { bg = "#2d1b45", fg = "#c4a7e7", bold = true, ctermbg = 54, ctermfg = 183, cterm = { bold = true } })
    d("PtrmTermHost", { bg = "#5fb3a1", fg = "#1d2021", bold = true, ctermbg = 73, ctermfg = 234, cterm = { bold = true } })
  end
  -- The model's thinking in the LLM pane.
  d("PtrmThink", { fg = light and "#8a8a7a" or "#7c7a70", italic = true, ctermfg = 244, cterm = { italic = true } })
end
set_hl()
api.nvim_create_autocmd("ColorScheme", { callback = set_hl })

-- Painted at draw time on the chat buffer's input line, so it follows the prompt as the transcript grows.
local NS_PROMPT = api.nvim_create_namespace("ptrm_prompt")
api.nvim_set_decoration_provider(NS_PROMPT, {
  on_win = function(_, _, buf) return S ~= nil and S.bufs ~= nil and buf == S.bufs.chat end,
  on_line = function(_, _, buf, row)
    if row ~= api.nvim_buf_line_count(buf) - 1 then return end
    local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    if line:sub(1, #PROMPT) == PROMPT then
      local col = 0
      for _, part in ipairs(PROMPT_PARTS) do
        api.nvim_buf_set_extmark(buf, NS_PROMPT, row, col, { end_col = col + #part[1], hl_group = part[2],
          ephemeral = true, priority = 200 })
        col = col + #part[1]
      end
    end
  end,
})

local function notify(msg, level) vim.notify("ptrm: " .. msg, level or vim.log.levels.INFO) end

local LOAD_CWD = fn.getcwd()

local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function valid_buf(b) return b and api.nvim_buf_is_valid(b) end
local function send(msg)
  if S and S.job then fn.chansend(S.job, vim.json.encode(msg) .. "\n") end
end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end
local function in_tab() return S ~= nil and S.tab ~= nil and api.nvim_tabpage_is_valid(S.tab)
  and api.nvim_get_current_tabpage() == S.tab end
local function win_in_tab(w) return valid_win(w) and S.tab ~= nil and api.nvim_win_get_tabpage(w) == S.tab end

--- The table file, as VimPage_ptrm picks it: <pages>/<hostname>.ptrm.md when it exists, else <pages>/ptrm.md.
local function table_path()
  if vim.g.ptrm_table then return native(fn.fnamemodify(fn.expand(vim.g.ptrm_table), ":p")) end
  local host = vim.g.hostname or vim.trim(fn.system("hostname"))
  local own = fn.expand("~/echo-home/.vim/pages/" .. host .. ".ptrm.md")
  return native(fn.filereadable(own) == 1 and own or fn.expand("~/echo-home/.vim/pages/ptrm.md"))
end

--- The LOCAL_PORT of a ptrm.md table row: "| host | 8081 | 127.0.0.1 | 8080 | notes |" -> 8081.
local function row_port(line) -- the LOCAL_PORT of a table line (also ~~8090~~, #8090, 8090 disabled)
  local cell = (line or ""):match("^%s*|[^|]*|([^|]*)|")
  return cell and tonumber(cell:match("^[%s~#!%-]*(%d+)")) or nil
end

-- ---------------------------------------------------------------- buffers
local function scroll_to_end(buf)
  for _, w in ipairs(api.nvim_list_wins()) do
    if api.nvim_win_get_buf(w) == buf and w ~= api.nvim_get_current_win() then
      pcall(api.nvim_win_set_cursor, w, { api.nvim_buf_line_count(buf), 0 })
    end
  end
end

local function append(key, text, hl)
  if not S then return end
  local buf = S.bufs[key]
  if not valid_buf(buf) then return end
  local lines = vim.split(text or "", "\n", { plain = true })
  local at
  if key == "chat" then
    at = api.nvim_buf_line_count(buf) - 1 -- keep the prompt line last
  else
    at = api.nvim_buf_line_count(buf)
    if at == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == "" then at = 0 end
  end
  vim.bo[buf].modifiable = true
  if key == "chat" then
    api.nvim_buf_set_lines(buf, at, at, false, lines)
  else
    api.nvim_buf_set_lines(buf, at, at == 0 and 1 or at, false, lines)
  end
  if key ~= "chat" then vim.bo[buf].modifiable = false end
  if hl then
    for i = 0, #lines - 1 do
      pcall(api.nvim_buf_set_extmark, buf, NS, at + i, 0, { line_hl_group = hl, priority = 50 })
    end
  end
  scroll_to_end(buf)
end

--- Streamed LLM text: continues the line the current stream writes (S.raw_row), which starts on a fresh line.
--- hl: the piece's highlight (the model's thinking: PtrmThink).
local function append_raw(key, text, hl)
  local buf = S.bufs[key]
  local n = api.nvim_buf_line_count(buf)
  vim.bo[buf].modifiable = true
  if not (S.raw_row and S.raw_row < n) then
    local empty = n == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == ""
    if not empty then
      api.nvim_buf_set_lines(buf, n, n, false, { "" })
      n = n + 1
    end
    S.raw_row = n - 1
  end
  local parts = vim.split(text or "", "\n", { plain = true })
  local head = api.nvim_buf_get_lines(buf, S.raw_row, S.raw_row + 1, false)[1] or ""
  local row0, col0 = S.raw_row, #head
  -- inserted at the end of the line (not the line replaced): the highlights of the earlier pieces stay where they are
  api.nvim_buf_set_text(buf, row0, col0, row0, col0, parts)
  S.raw_row = row0 + #parts - 1
  if hl then
    local end_col = #parts == 1 and col0 + #parts[1] or #parts[#parts]
    local last = S.raw_mark
    if last and last.buf == buf and last.hl == hl and last.erow == row0 and last.ecol == col0 then
      -- the same thinking goes on: its highlight grows (one mark per thought, not one per streamed token)
      pcall(api.nvim_buf_set_extmark, buf, NS, last.row, last.col,
        { id = last.id, end_row = S.raw_row, end_col = end_col, hl_group = hl, priority = 60 })
    else
      local ok, id = pcall(api.nvim_buf_set_extmark, buf, NS, row0, col0,
        { end_row = S.raw_row, end_col = end_col, hl_group = hl, priority = 60 })
      last = ok and { buf = buf, hl = hl, id = id, row = row0, col = col0 } or nil
    end
    if last then last.erow, last.ecol = S.raw_row, end_col end
    S.raw_mark = last
  else
    S.raw_mark = nil
  end
  vim.bo[buf].modifiable = false
  scroll_to_end(buf)
end

-- ---------------------------------------------------------------- headers
local function net_state()
  local net = S and S.state and S.state.net
  return type(net) == "table" and net or {}
end
local function joined() return net_state().joined == true end

local ICON = { up = "✅", loading = "⏳", down = "🟠", lost = "❌", off = "⚪", busy = "⛔", ["local"] = "🏠", other = "🔀",
  stalled = "⌛", error = "❗", unlisted = "🔀", disabled = "🚫" }

local function counts_text(c)
  local parts = {}
  for _, k in ipairs({ "up", "loading", "local", "down", "stalled", "error", "lost", "busy", "other", "unlisted", "off", "disabled" }) do
    if type(c) == "table" and (c[k] or 0) > 0 then parts[#parts + 1] = ICON[k] .. " " .. c[k] .. " " .. k end
  end
  return #parts > 0 and table.concat(parts, " · ") or "no rows"
end

--- PTRM: the agent's status.
local function main_status()
  local st = S and S.state or {}
  local parts = {}
  parts[1] = st.busy and ("● " .. tostring(st.phase or "busy")) or (S and S.job and "○ idle" or "✖ stopped")
  if (st.queue or 0) > 0 then parts[#parts + 1] = st.queue .. " queued" end
  if st.auto then parts[#parts + 1] = "AUTO" end
  if st.heal == false then parts[#parts + 1] = "no healing" end
  if not joined() then parts[#parts + 1] = "🌐 off" end
  if st.approval and st.approval ~= vim.NIL then parts[#parts + 1] = "❓ y/n/a" end
  return table.concat(parts, " · ")
end

local function net_status()
  local net = net_state()
  return "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
    .. " [" .. tostring(net.state) .. "]" .. ((net.members and net.connected) and (" · " .. net.members .. " members") or "")
end

local function llm_status()
  local st = S and S.state or {}
  local model = st.model
  return "raw output" .. (st.think == true and " · 💭 thinking" or "") .. ((st.llm and st.llm ~= vim.NIL) and (" · " .. tostring(st.llm)) or "")
    .. ((model and model ~= vim.NIL) and (" · " .. fn.fnamemodify(tostring(model), ":t")) or " · not reachable")
end

local STATUS = { chat = main_status, log = llm_status, iface = net_status }

local function fit(s, width)
  if fn.strdisplaywidth(s) <= width then return s end
  local n = fn.strchars(s)
  while n > 0 and fn.strdisplaywidth(fn.strcharpart(s, 0, n)) > width - 1 do n = n - 1 end
  return fn.strcharpart(s, 0, n) .. "…"
end

local function bar(color, badge, text, w)
  local room = api.nvim_win_get_width(w) - fn.strdisplaywidth(badge) - 3
  return "%#Ptrm" .. color .. "Badge# " .. esc(badge) .. " %#Ptrm" .. color .. "Bar# " .. esc(fit(text, math.max(1, room)))
end

local function stats_of_win(w)
  for _, st in ipairs(S.stats) do
    if valid_win(w) and api.nvim_win_get_buf(w) == st.buf then return st end
  end
end

--- The terminal panes: monitors and teach terminals in the STATS grid; a shell opened by t (dock "dash") below DASH.
local function grid_list() return vim.tbl_filter(function(st) return st.dock ~= "dash" end, S.stats) end
local function term_list() return vim.tbl_filter(function(st) return st.dock == "dash" end, S.stats) end
local function pane_wins()
  local out = {}
  for _, key in ipairs({ "stats", "terms" }) do
    for _, w in ipairs(S.wins[key] or {}) do out[#out + 1] = w end
  end
  return out
end

local function refresh_bars()
  if not S then return end
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then vim.wo[w].winbar = bar(COLOR[key], BADGE[key], STATUS[key](), w) end
  end
  local w = S.wins.dash
  if valid_win(w) then
    local st = S.state or {}
    local file = st.table and st.table ~= vim.NIL and fn.fnamemodify(tostring(st.table), ":t") or "ptrm.md"
    vim.wo[w].winbar = bar("Dash", "DASH", counts_text(st.counts) .. " · " .. file, w)
  end
  for _, sw in ipairs(pane_wins()) do
    if valid_win(sw) then
      local st = stats_of_win(sw)
      if st and st.tool == "shell" then
        -- a terminal opened by t: ' TERM ' · ' <username> ' (inverted) · ' <hostname> ' in its own colour to the edge
        local user, host = tostring(st.name or st.title):match("^(.*)@([^@]*)$")
        user, host = user or "", host or tostring(st.name or st.title)
        local live = st.job ~= nil
        local tail = live and "" or (" · exited " .. tostring(st.code or "") .. " · R restart · x close")
        local room = api.nvim_win_get_width(sw) - 6 - (user ~= "" and fn.strdisplaywidth(user) + 2 or 0) - 2
        vim.wo[sw].winhighlight = "WinBar:PtrmTermHost,WinBarNC:PtrmTermHost"
        vim.wo[sw].winbar = "%#" .. (live and "PtrmTermBadge" or "PtrmStatsOffBadge") .. "# TERM "
          .. (user ~= "" and ("%#PtrmTermUser# " .. esc(user) .. " ") or "")
          .. "%#PtrmTermHost# " .. esc(fit(host .. tail, math.max(1, room))) .. "%="
      elseif st then
        local live = st.job ~= nil
        vim.wo[sw].winhighlight = live and "WinBar:PtrmStatsBar,WinBarNC:PtrmStatsBar"
          or "WinBar:PtrmStatsOffBar,WinBarNC:PtrmStatsOffBar"
        vim.wo[sw].winbar = bar(live and "Stats" or "StatsOff", "STATS", st.title
          .. (live and "" or (" · exited " .. tostring(st.code or "") .. " · R restart · x close")), sw)
      else
        vim.wo[sw].winhighlight = "WinBar:PtrmStatsBar,WinBarNC:PtrmStatsBar"
        vim.wo[sw].winbar = bar("Stats", "STATS", "live monitors (s on a DASH row · S this machine · :PtrmStats)", sw)
      end
    end
  end
end

-- ---------------------------------------------------------------- the top row: DASH · STATS
local STATS_NONE = { "STATS", "",
  "Live monitors, each a terminal on a host over ssh -t (htop, btop, nvtop, nvidia-smi …) or on this",
  "machine (S), as many as you like, in a grid; each buffer is named <user>@<host>.", "",
  "  s on a DASH row     the row's host            S on DASH   this machine",
  "  t on a DASH row     a shell on that host, below DASH, to type commands there (:PtrmTerm [host])",
  "  :PtrmStats [host] [tool]   tool: top htop btop nvtop macmon gpu",
  "  or ask: \"show me the GPU of hostB\"", "",
  "In a monitor: x close it · R restart it · i type into it (<C-\\><C-n> back)",
  "g:ptrm_stats lists the monitors opened at start ({'hosts'}: each host of the table)." }

local function stats_placeholder()
  if not valid_buf(S.bufs.statsnone) then
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    api.nvim_buf_set_lines(b, 0, -1, false, STATS_NONE)
    vim.bo[b].modifiable = false
    S.bufs.statsnone = b
    M._pane_keys(b)
  end
  return S.bufs.statsnone
end

local function pane_opts(w, wrap)
  vim.wo[w].wrap = wrap
  vim.wo[w].number, vim.wo[w].relativenumber, vim.wo[w].list = false, false, false
  vim.wo[w].signcolumn, vim.wo[w].foldcolumn = "no", "0"
  vim.wo[w].cursorline = false
end

local function sizes()
  if not (S and in_tab()) then return end
  local avail = vim.o.lines - vim.o.cmdheight - 1
  local h = tonumber(vim.g.ptrm_bottom_height)
  if not h then
    h = math.floor(avail * 0.38) - 2 -- the default: 38% of the screen, less 2 lines
  elseif h < 1 then
    h = math.floor(avail * h)
  end
  h = math.max(6, math.min(math.floor(h), avail - 8))
  local cols = vim.o.columns
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then
      pcall(api.nvim_win_set_height, w, h)
      vim.wo[w].winfixheight = true
    end
  end
  if valid_win(S.wins.log) then
    pcall(api.nvim_win_set_width, S.wins.log, math.floor(cols * (tonumber(vim.g.ptrm_llm_width) or 0.34)))
  end
  if valid_win(S.wins.iface) then
    pcall(api.nvim_win_set_width, S.wins.iface, math.floor(cols * (tonumber(vim.g.ptrm_iface_width) or 0.26)))
  end
  local dw = math.floor(cols * (tonumber(vim.g.ptrm_dash_width) or 0.5))
  if valid_win(S.wins.dash) then pcall(api.nvim_win_set_width, S.wins.dash, dw) end
  -- the shells (t) below DASH: g:ptrm_term_height (0.4) of the top row together (a fraction, or rows), equal shares
  local terms = vim.tbl_filter(valid_win, S.wins.terms or {})
  local top_rows = avail - h - 1
  if #terms > 0 and valid_win(S.wins.dash) then
    local n = #terms
    local th = tonumber(vim.g.ptrm_term_height) or 0.4
    if th < 1 then th = math.floor(top_rows * th) end
    th = math.max(4 * n, math.min(math.floor(th), top_rows - 8)) -- the shells' rows, their status lines included
    local each = math.max(3, math.floor(th / n) - 1)
    local dash_h = math.max(5, top_rows - (n + 1) - n * each)
    pcall(api.nvim_win_set_height, S.wins.dash, dash_h) -- what DASH gives or takes goes to the shell below it …
    for i = 1, n - 1 do pcall(api.nvim_win_set_height, terms[i], each) end -- … and the last shell takes the rest
  end
  -- the STATS grid: equal columns, equal rows in each column
  local grid = S.grid or {}
  local ncol = #grid
  if ncol > 0 then
    local area = cols - dw - 1
    local cw = math.max(10, math.floor((area - (ncol - 1)) / ncol))
    local top = valid_win(S.wins.dash) and api.nvim_win_get_height(S.wins.dash) or top_rows
    for _, w in ipairs(terms) do top = top + api.nvim_win_get_height(w) + 2 end -- + its status line and winbar
    for c, col in ipairs(grid) do
      if c < ncol and valid_win(col[1]) then pcall(api.nvim_win_set_width, col[1], cw) end
      local k = #col
      local rh = math.max(3, math.floor((top - (k - 1)) / k))
      for r = 1, k - 1 do
        if valid_win(col[r]) then pcall(api.nvim_win_set_height, col[r], rh) end
      end
    end
  end
end

--- A STATS pane's buffer is named <user>@<host> (the backend's "name"), so the panes of many hosts stay apart (:ls,
--- the status line). A second pane on the same host adds what it runs: <user>@<host> [htop], [teach 8090] …
--- A buffer Vim keeps (unloaded, unlisted) under a name a rename gave up: the alternate file. It holds the name.
local function husk(b)
  return not api.nvim_buf_is_loaded(b) and not vim.bo[b].buflisted and vim.bo[b].buftype == ""
end

--- nvim_buf_set_name without leaving that husk behind under the old name (term://…, an earlier <user>@<host>).
local function set_name(buf, name)
  local old = api.nvim_buf_get_name(buf)
  local ok = pcall(api.nvim_buf_set_name, buf, name)
  if ok and old ~= "" then
    for _, b in ipairs(api.nvim_list_bufs()) do
      if b ~= buf and api.nvim_buf_get_name(b) == old and husk(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
    end
  end
  return ok
end

local function name_stats(st)
  if not (st.name and valid_buf(st.buf)) then return end
  local function taken(n)
    local full = fn.fnamemodify(n, ":p")
    for _, b in ipairs(api.nvim_list_bufs()) do
      if b ~= st.buf and api.nvim_buf_get_name(b) == full then
        if not husk(b) then return true end
        pcall(api.nvim_buf_delete, b, { force = true }) -- a husk from an earlier rename: the name is free
      end
    end
    return false
  end
  local what = st.teach and ("teach " .. tostring(st.key):match("%d+$")) or tostring(st.tool or "stats")
  local names = { st.name, st.name .. " [" .. what .. "]" }
  for i = 2, 9 do names[#names + 1] = st.name .. " [" .. what .. " " .. i .. "]" end
  for _, n in ipairs(names) do
    if api.nvim_buf_get_name(st.buf) == fn.fnamemodify(n, ":p") then return end
    if not taken(n) and set_name(st.buf, n) then return end
  end
end

--- Start the monitor of each STATS pane that shows one and runs nothing yet (a terminal needs its window: its size).
local function start_monitors()
  for _, w in ipairs(pane_wins()) do
    local st = valid_win(w) and stats_of_win(w)
    if st and not st.job and not st.started then
      st.started = true
      api.nvim_win_call(w, function()
        local opts = {
          on_exit = function(_, code)
            vim.schedule(function()
              if not S then return end
              st.job, st.code = nil, code
              if st.teach then send({ t = "op", op = "stats_closed", key = st.key }) end
              refresh_bars()
            end)
          end,
        }
        local ok, job
        if fn.has("nvim-0.11") == 1 then
          opts.term = true
          ok, job = pcall(fn.jobstart, st.cmd, opts)
        else
          ok, job = pcall(fn.termopen, st.cmd, opts)
        end
        if ok and job and job > 0 then
          st.job = job
          name_stats(st) -- the terminal named its buffer term://…: <user>@<host> again
          if st.input then -- typed into the shell once it is up (a teach terminal's setup, a tool on a Windows host)
            vim.defer_fn(function() if st.job == job then pcall(fn.chansend, job, st.input) end end, 1500)
          end
        else
          st.code = "cannot start: " .. tostring(job)
        end
      end)
      pane_opts(w, false)
    end
  end
  refresh_bars()
end

--- Forget the STATS panes whose buffer is gone: a key pressed in a terminal whose process exited wipes it (Neovim's
--- terminal), and so do :bwipeout and the like. Returns how many were dropped.
local function prune_stats()
  if not S then return 0 end
  local kept = {}
  for _, st in ipairs(S.stats) do
    if valid_buf(st.buf) then kept[#kept + 1] = st end
  end
  local gone = #S.stats - #kept
  if gone > 0 then
    S.stats = kept
    S.stats_dirty, S.terms_dirty = true, true
  end
  return gone
end

--- (Re)build the STATS grid right of DASH: columns of g:ptrm_stats_rows panes (buffers and monitors live on).
local function build_stats()
  prune_stats()
  local W = S.wins
  local old = W.stats or {}
  W.stats, S.grid = {}, {} -- detached first: WinClosed (below) edits W.stats while these close
  for _, w in ipairs(old) do
    if valid_win(w) then pcall(api.nvim_win_close, w, true) end
  end
  S.stats_dirty = false
  if not valid_win(W.dash) then return end
  local list = grid_list()
  if #list == 0 then
    local w = api.nvim_open_win(stats_placeholder(), false, { split = "right", win = W.dash })
    pane_opts(w, false)
    W.stats = { w }
    S.grid = { { w } }
    return
  end
  local rows = math.max(1, tonumber(vim.g.ptrm_stats_rows) or 2)
  local ncol = math.ceil(#list / rows)
  local left = W.dash
  for c = 1, ncol do -- the column heads first, left to right …
    local w = api.nvim_open_win(list[(c - 1) * rows + 1].buf, false, { split = "right", win = left })
    S.grid[c] = { w }
    left = w
  end
  for c = 1, ncol do -- … then each column's other panes below its head
    for r = 2, rows do
      local st = list[(c - 1) * rows + r]
      if not st then break end
      local col = S.grid[c]
      col[#col + 1] = api.nvim_open_win(st.buf, false, { split = "below", win = col[#col] })
    end
  end
  for _, col in ipairs(S.grid) do
    for _, w in ipairs(col) do
      W.stats[#W.stats + 1] = w
      pane_opts(w, false)
    end
  end
end

--- The shells opened by t, stacked below DASH in its column (built after the STATS grid, so the grid keeps the whole
--- height of the top row).
local function close_terms()
  local old = S.wins.terms or {}
  S.wins.terms = {} -- detached first: WinClosed (below) edits the list while these close
  for _, w in ipairs(old) do
    if valid_win(w) then pcall(api.nvim_win_close, w, true) end
  end
end

local function build_terms()
  local W = S.wins
  close_terms()
  S.terms_dirty = false
  if not valid_win(W.dash) then return end
  local above = W.dash
  for _, st in ipairs(term_list()) do
    local ok, w = pcall(api.nvim_open_win, st.buf, false, { split = "below", win = above })
    if ok then
      W.terms[#W.terms + 1] = w
      pane_opts(w, false)
      above = w
    end
  end
end

--- Build what is missing of the tab: DASH | STATS on top (the shells of t below DASH), PTRM · LLM · IFACE (IFACE while
--- joined) below.
local function layout()
  if not S then return end
  if not (S.tab and api.nvim_tabpage_is_valid(S.tab)) then
    vim.cmd("tabnew")
    S.tab = api.nvim_get_current_tabpage()
    local scratch = api.nvim_get_current_buf()
    local w = api.nvim_get_current_win()
    api.nvim_win_set_buf(w, S.bufs.dash)
    if api.nvim_buf_get_name(scratch) == "" and scratch ~= S.bufs.dash then pcall(api.nvim_buf_delete, scratch, { force = true }) end
    S.wins = { dash = w, stats = {} }
    S.stats_dirty = true
  end
  if not in_tab() then return end
  local W = S.wins
  for _, key in ipairs({ "dash", "chat", "log", "iface" }) do
    if W[key] and not win_in_tab(W[key]) then W[key] = nil end
  end
  local alive = {}
  for _, w in ipairs(W.stats or {}) do if win_in_tab(w) then alive[#alive + 1] = w end end
  if #alive ~= #(W.stats or {}) then S.stats_dirty = true end
  W.stats = alive
  local talive = {}
  for _, w in ipairs(W.terms or {}) do if win_in_tab(w) then talive[#talive + 1] = w end end
  if #talive ~= #(W.terms or {}) then S.terms_dirty = true end
  W.terms = talive
  if not W.chat then
    W.chat = api.nvim_open_win(S.bufs.chat, false, { split = "below", win = -1 })
    pcall(api.nvim_win_set_cursor, W.chat, { api.nvim_buf_line_count(S.bufs.chat), 0 })
  end
  if not W.dash then
    W.dash = api.nvim_open_win(S.bufs.dash, false, { split = "above", win = -1 })
    S.stats_dirty = true
  end
  if not W.log then
    W.log = api.nvim_open_win(S.bufs.log, false, { split = "right", win = W.chat })
    pcall(api.nvim_win_set_cursor, W.log, { api.nvim_buf_line_count(S.bufs.log), 0 })
  end
  if joined() and not W.iface then
    W.iface = api.nvim_open_win(S.bufs.iface, false, { split = "right", win = W.log })
    pcall(api.nvim_win_set_cursor, W.iface, { api.nvim_buf_line_count(S.bufs.iface), 0 })
  elseif not joined() and W.iface then
    if api.nvim_get_current_win() == W.iface then api.nvim_set_current_win(W.chat) end
    pcall(api.nvim_win_close, W.iface, true)
    W.iface = nil
  end
  if S.stats_dirty or #W.stats ~= math.max(1, #grid_list()) then
    close_terms() -- the grid is split off DASH at its full height: the shells go first, come back after
    build_stats()
    S.terms_dirty = true
  end
  if S.terms_dirty or #W.terms ~= #term_list() then build_terms() end
  for _, key in ipairs(BOTTOM) do
    local w = W[key]
    if valid_win(w) then
      pane_opts(w, true)
      vim.wo[w].winhighlight = ("WinBar:Ptrm%s,WinBarNC:Ptrm%s"):gsub("%%s", COLOR[key] .. "Bar")
    end
  end
  pane_opts(W.dash, false)
  vim.wo[W.dash].cursorline = true
  vim.wo[W.dash].winhighlight = "WinBar:PtrmDashBar,WinBarNC:PtrmDashBar"
  sizes()
  start_monitors()
  refresh_bars()
end

local function refresh()
  if not S then return end
  if in_tab() then layout() else refresh_bars() end
end

--- ptrm.md open in a buffer: its LOCAL_PORT cells as in DASH (up: dark green bold; disabled: struck through) and
--- REMOTE_PORT cells that are not the LOCAL_PORT in dark orange bold. Repainted with each DASH update.
local function mark_table(path, marks)
  if not path or path == vim.NIL then return end
  local full = fn.fnamemodify(tostring(path), ":p")
  local by = {}
  for _, m in ipairs(marks or {}) do by[tonumber(m.port)] = m end
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) and api.nvim_buf_get_name(b) ~= "" and fn.fnamemodify(api.nvim_buf_get_name(b), ":p") == full then
      api.nvim_buf_clear_namespace(b, NS_TABLE, 0, -1)
      local in_table = false
      for i, line in ipairs(api.nvim_buf_get_lines(b, 0, -1, false)) do
        if not line:match("^%s*|") then
          in_table = false
        else
          local cells, pos = {}, line:find("|", 1, true)
          while pos do
            local nxt = line:find("|", pos + 1, true)
            if not nxt then break end
            cells[#cells + 1] = { pos + 1, nxt - 1 } -- the cell's text: 1-based, inclusive
            pos = nxt
          end
          local function text(ci) return cells[ci] and line:sub(cells[ci][1], cells[ci][2]) or "" end
          if vim.trim(text(1)):upper() == "SSH_HOST" then in_table = true end
          local m = in_table and by[tonumber(text(2):match("^[%s~#!%-]*(%d+)") or "")]
          local function paint(ci, group)
            local t = text(ci)
            local a = t:find("%S")
            local z = #t - ((t:reverse():find("%S")) or 1) + 1
            if a then
              pcall(api.nvim_buf_set_extmark, b, NS_TABLE, i - 1, cells[ci][1] - 1 + a - 1,
                { end_col = cells[ci][1] - 1 + z, hl_group = group, priority = 150 })
            end
          end
          if m then
            if m.host_off then paint(1, "PtrmPortOff") end -- its host is disabled (Xh)
            if m.disabled then paint(2, "PtrmPortOff") elseif m.up then paint(2, "PtrmPortUp") end
            if m.differs and text(4):match("%d") then paint(4, "PtrmPortDiff") end
          end
        end
      end
    end
  end
end

--- The DASH: the backend's lines, its highlights, and which port/host each line is about (cursor line kept).
local function on_dash(msg)
  local b = S.bufs.dash
  local w = S.wins.dash
  local keep = valid_win(w) and api.nvim_win_get_buf(w) == b and api.nvim_win_get_cursor(w) or nil
  vim.bo[b].modifiable = true
  api.nvim_buf_set_lines(b, 0, -1, false, msg.lines or {})
  vim.bo[b].modifiable = false
  api.nvim_buf_clear_namespace(b, NS_DASH, 0, -1)
  for _, h in ipairs(msg.hls or {}) do
    pcall(api.nvim_buf_set_extmark, b, NS_DASH, h[1], h[2], { end_col = h[3], hl_group = h[4], priority = 90 })
  end
  S.dash_rows = msg.rows or {}
  pcall(mark_table, msg.table, msg.marks)
  if keep then pcall(api.nvim_win_set_cursor, w, { math.min(keep[1], api.nvim_buf_line_count(b)), keep[2] }) end
  refresh_bars()
end

--- A monitor for STATS (the backend chose the tool and the command): a new pane, unless the same one runs.
local function on_stats(msg)
  prune_stats()
  local function show(st)
    if in_tab() then
      for _, w in ipairs(pane_wins()) do
        if stats_of_win(w) == st then api.nvim_set_current_win(w) end
      end
    end
  end
  local function typing(st) -- a teach terminal or a shell (t): the user types there; a shell goes to terminal mode
    show(st)
    if msg.insert and in_tab() and api.nvim_get_current_buf() == st.buf then
      vim.cmd("startinsert")
      notify(st.title .. ": type your commands — <C-\\><C-n> back to normal mode, x closes the pane")
    else
      notify(st.title .. ": a terminal on the host — i to type, <C-\\><C-n> back", vim.log.levels.WARN)
    end
  end
  for _, st in ipairs(S.stats) do
    if st.key == msg.key and st.job then
      if msg.focus then return typing(st) end
      show(st)
      return notify(msg.title .. " is already in STATS")
    end
  end
  for _, st in ipairs(S.stats) do
    if st.key == msg.key then -- the same monitor / teach terminal / shell, exited: started again in its pane
      st.cmd, st.title, st.teach = msg.cmd, msg.title or msg.key, msg.teach == true
      st.input = msg.input ~= vim.NIL and msg.input or nil
      st.name = msg.name ~= vim.NIL and msg.name or st.name
      M.stats_restart(st)
      if msg.focus then typing(st) else show(st) end
      return
    end
  end
  local dock = (msg.dock == "dash" or msg.tool == "shell") and "dash" or nil
  local max = tonumber(vim.g.ptrm_stats_max) or 6
  local same = dock and term_list() or grid_list()
  while #same >= max do
    M.stats_close(same[1])
    same = dock and term_list() or grid_list()
  end
  local b = api.nvim_create_buf(false, true)
  vim.bo[b].bufhidden = "hide"
  local st = { key = msg.key, host = msg.host ~= vim.NIL and msg.host or nil, tool = msg.tool, cmd = msg.cmd,
    title = msg.title or msg.key, buf = b, teach = msg.teach == true, dock = dock,
    input = msg.input ~= vim.NIL and msg.input or nil, name = msg.name ~= vim.NIL and msg.name or nil }
  name_stats(st)
  M._stats_keys(b, st)
  table.insert(S.stats, st)
  if dock then S.terms_dirty = true else S.stats_dirty = true end
  if in_tab() then layout() end
  if msg.focus then typing(st) end -- a teach terminal, a shell: the user is asked to type there
end

-- ---------------------------------------------------------------- navigation
function M.focus(key)
  if not (S and S.wins) then return end
  local w = S.wins[key]
  if not valid_win(w) then w, key = S.wins.chat, "chat" end
  if not valid_win(w) then return end
  api.nvim_set_current_win(w)
  if key == "chat" then
    pcall(api.nvim_win_set_cursor, w, { api.nvim_buf_line_count(S.bufs.chat), 0 })
    vim.cmd("startinsert!")
  else
    vim.cmd("stopinsert")
  end
end

local function cycle(delta)
  if not (S and in_tab()) then return end
  local list = {}
  if valid_win(S.wins.dash) then list[#list + 1] = S.wins.dash end
  for _, w in ipairs(S.wins.terms or {}) do if valid_win(w) then list[#list + 1] = w end end
  for _, w in ipairs(S.wins.stats or {}) do if valid_win(w) then list[#list + 1] = w end end
  for _, key in ipairs(BOTTOM) do
    if valid_win(S.wins[key]) then list[#list + 1] = S.wins[key] end
  end
  if #list == 0 then return end
  local i = 1
  for k, w in ipairs(list) do if w == api.nvim_get_current_win() then i = k end end
  local w = list[(i - 1 + delta) % #list + 1]
  if w == S.wins.chat then return M.focus("chat") end
  vim.cmd("stopinsert")
  api.nvim_set_current_win(w)
end

function M.show()
  if not S then return M.start() end
  if not in_tab() then
    S.prev_tab = api.nvim_get_current_tabpage()
    S.anchor = api.nvim_get_current_win()
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) then api.nvim_set_current_tabpage(S.tab) end
  layout()
  M.focus("chat")
end

function M.back()
  if S and S.prev_tab and api.nvim_tabpage_is_valid(S.prev_tab) and S.prev_tab ~= S.tab then
    vim.cmd("stopinsert")
    api.nvim_set_current_tabpage(S.prev_tab)
    if valid_win(S.anchor) and api.nvim_win_get_tabpage(S.anchor) == S.prev_tab then pcall(api.nvim_set_current_win, S.anchor) end
  end
end

function M.toggle()
  if not S then return M.start() end
  if in_tab() then return M.back() end
  M.show()
end

-- ---------------------------------------------------------------- context & editor requests
local function anchor_win()
  if S and valid_win(S.anchor) and not (S.tab and api.nvim_win_get_tabpage(S.anchor) == S.tab) then return S.anchor end
  local cur = api.nvim_get_current_win()
  if S and S.tab and api.nvim_win_get_tabpage(cur) == S.tab then return nil end
  return cur
end

local function context(win)
  win = win or anchor_win()
  local ctx = { buffer = "", line = "" }
  if valid_win(win) then
    local buf = api.nvim_win_get_buf(win)
    local row = api.nvim_win_get_cursor(win)[1]
    ctx.buffer = api.nvim_buf_get_name(buf)
    ctx.line = api.nvim_buf_get_lines(buf, row - 1, row, false)[1] or ""
    ctx.port = row_port(ctx.line)
  end
  if S and S.dash_sel and S.dash_sel.port then ctx.dash_port = S.dash_sel.port end
  return ctx
end

local OPS = {}
function OPS.context() return context() end

function OPS.ex(p)
  local win = anchor_win() or api.nvim_get_current_win()
  return api.nvim_win_call(win, function() return api.nvim_exec2(p.command, { output = true }).output end)
end

function OPS.lua(p)
  local chunk, err = load(p.code, "ptrm-agent", "t")
  if not chunk then error(err) end
  local printed = {}
  local orig = print
  print = function(...) printed[#printed + 1] = table.concat(vim.tbl_map(tostring, { ... }), " ") end
  local win = anchor_win() or api.nvim_get_current_win()
  local ok, res = pcall(api.nvim_win_call, win, chunk)
  print = orig
  if not ok then error(res) end
  local out = table.concat(printed, "\n")
  if res ~= nil then out = out .. (out ~= "" and "\n" or "") .. vim.inspect(res) end
  return out
end

--- The agent changed the table: buffers showing it reload (unless you have unsaved changes there).
function OPS.reload(p)
  local path = p.path and fn.fnamemodify(p.path, ":p")
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) and path and fn.fnamemodify(api.nvim_buf_get_name(b), ":p") == path then
      if vim.bo[b].modified then
        notify("the table changed on disk; your buffer has unsaved changes (:e! to reload)", vim.log.levels.WARN)
      else
        pcall(vim.cmd, "checktime " .. b)
      end
    end
  end
end

local function handle_req(msg)
  local f = OPS[msg.op]
  local ok, out = pcall(function()
    if not f then error("unknown op " .. tostring(msg.op)) end
    return f(msg)
  end)
  if msg.id and msg.id ~= 0 then
    send({ t = "res", id = msg.id, ok = ok, out = ok and out or nil, err = (not ok) and tostring(out) or nil })
  end
end

-- ---------------------------------------------------------------- backend messages
local function on_message(msg)
  if msg.t == "w" then
    local b = (msg.b == "iface" or msg.b == "log") and msg.b or "chat"
    if msg.raw then
      append_raw(b, msg.text, msg.hl ~= vim.NIL and msg.hl or nil)
    else
      if msg.eos then S.raw_row = nil end
      append(b, msg.text, msg.hl ~= vim.NIL and msg.hl or nil)
    end
  elseif msg.t == "state" then
    local was = joined()
    S.state = msg
    if joined() ~= was then refresh() else refresh_bars() end
  elseif msg.t == "dash" then
    on_dash(msg)
  elseif msg.t == "stats" then
    on_stats(msg)
  elseif msg.t == "req" then
    handle_req(msg)
  elseif msg.t == "show" then
    M.show()
  elseif msg.t == "alert" then -- incidents, remote hands: seen from any tab
    notify(msg.text or "", vim.log.levels.WARN)
  elseif msg.t == "exit" then
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
  append("chat", "ptrm stopped (exit " .. tostring(code) .. "). :Ptrm restarts it (the tunnels stay open).",
    code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh()
end

-- ---------------------------------------------------------------- input
local function submit(text, ctx)
  text = vim.trim(text or "")
  if text == "" then return end
  if not (S and S.job) then return notify("not running (:Ptrm)", vim.log.levels.WARN) end
  send({ t = "input", text = text, ctx = ctx or context() })
end

--- The DASH line under the cursor: {port=, host=} (nil on a line about nothing).
local function dash_row()
  if not (S and S.dash_rows) then return nil end
  return S.dash_rows[tostring(api.nvim_win_get_cursor(0)[1])]
end

local function common_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
  vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
  vim.keymap.set("n", "q", function() M.back() end, o)
  vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
end

function M._pane_keys(b)
  common_keys(b)
  vim.keymap.set("n", "i", function() M.focus("chat") end, { buffer = b, silent = true, nowait = true })
end

function M._dash_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  local function op(name, need)
    return function()
      local r = dash_row()
      if not r then return notify("no port or host on this line") end
      if need == "port" and not r.port then return notify("no port on this line") end
      if need == "host" and not r.host then return notify("no host on this line") end
      send({ t = "op", op = name, port = r.port, host = r.host })
    end
  end
  vim.keymap.set("n", "<CR>", function()
    local r = dash_row()
    if not r then return end
    if r.incident then return send({ t = "op", op = "incident", incident = r.incident }) end
    if r.port then send({ t = "op", op = "check", port = r.port }) else send({ t = "op", op = "reach", host = r.host }) end
  end, o)
  vim.keymap.set("n", "D", op("diagnose"), o)
  vim.keymap.set("n", "B", op("bringup", "port"), o)
  vim.keymap.set("n", "T", op("teach", "port"), o)
  -- X: disable / enable the row (on a HOSTS line: the host); Xh: the host of the line. X waits 'timeoutlen' for the h.
  local ow = { buffer = b, silent = true }
  vim.keymap.set("n", "X", function()
    local r = dash_row()
    if not r then return notify("no port or host on this line") end
    if r.port then return send({ t = "op", op = "disable", port = r.port }) end
    send({ t = "op", op = "disable_host", host = r.host })
  end, ow)
  vim.keymap.set("n", "Xh", function()
    local r = dash_row()
    if not (r and r.host and r.host ~= "") then return notify("no host on this line (SSH_HOST empty and none chosen)") end
    send({ t = "op", op = "disable_host", host = r.host })
  end, ow)
  vim.keymap.set("n", "o", op("open", "port"), o)
  vim.keymap.set("n", "x", op("close", "port"), o)
  vim.keymap.set("n", "R", op("reopen", "port"), o)
  vim.keymap.set("n", "d", op("discover", "host"), o)
  vim.keymap.set("n", "h", op("history"), o)
  vim.keymap.set("n", "s", function()
    local r = dash_row()
    if not (r and r.host) then return notify("no host on this line") end
    send({ t = "op", op = "stats", host = r.host })
  end, o)
  vim.keymap.set("n", "S", function() send({ t = "op", op = "stats" }) end, o)
  vim.keymap.set("n", "t", function() -- a shell on the row's host (a HOSTS line: that host), to type commands there
    local r = dash_row()
    if not r then return notify("no port or host on this line") end
    if (not r.host or r.host == "") and not r.port then return notify("no host on this line") end
    send({ t = "op", op = "term", host = (r.host ~= "" and r.host) or nil, port = r.port })
  end, o)
  vim.keymap.set("n", "r", function() send({ t = "op", op = "refresh" }) end, o)
  vim.keymap.set("n", "e", function() M.edit_table() end, o)
  vim.keymap.set("n", "a", function() -- ask about this row: the input starts with it
    local r = dash_row()
    M.focus("chat")
    if r then
      local chat = S.bufs.chat
      local n = api.nvim_buf_line_count(chat)
      api.nvim_buf_set_lines(chat, n - 1, n, false, { PROMPT .. (r.port and ("port " .. r.port .. ": ") or (r.host .. ": ")) })
      vim.cmd("startinsert!")
    end
  end, o)
  vim.keymap.set("n", "i", function() M.focus("chat") end, o)
  common_keys(b)
end

function M._stats_keys(b, st)
  local o = { buffer = b, silent = true, nowait = true }
  api.nvim_create_autocmd("BufWipeout", { buffer = b, once = true, callback = function()
    vim.schedule(function() -- the pane goes with its buffer; the grid closes the gap
      if S and prune_stats() > 0 and in_tab() then layout() end
    end)
  end })
  vim.keymap.set("n", "x", function() M.stats_close(st) end, o)
  vim.keymap.set("n", "R", function() M.stats_restart(st) end, o)
  common_keys(b)
end

function M.stats_close(st)
  if not S then return end
  if st.teach then send({ t = "op", op = "stats_closed", key = st.key }) end
  for i, x in ipairs(S.stats) do
    if x == st then table.remove(S.stats, i) break end
  end
  if st.job then pcall(fn.jobstop, st.job) end
  st.job = nil
  local b = st.buf
  if st.dock == "dash" then S.terms_dirty = true else S.stats_dirty = true end
  if in_tab() then layout() end
  if valid_buf(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
end

function M.stats_restart(st)
  if st.job then pcall(fn.jobstop, st.job) end
  local old = st.buf
  if valid_buf(old) then set_name(old, api.nvim_buf_get_name(old) .. " (closed)") end -- its name goes to the new one
  st.buf = api.nvim_create_buf(false, true) -- a terminal buffer cannot host a second job
  vim.bo[st.buf].bufhidden = "hide"
  name_stats(st)
  st.job, st.code, st.started = nil, nil, false
  M._stats_keys(st.buf, st)
  if st.dock == "dash" then S.terms_dirty = true else S.stats_dirty = true end
  if in_tab() then layout() end
  if valid_buf(old) then pcall(api.nvim_buf_delete, old, { force = true }) end
end

local function create_buffers()
  local chat = api.nvim_create_buf(false, true)
  vim.bo[chat].buftype = "prompt"
  vim.bo[chat].bufhidden = "hide"
  vim.bo[chat].filetype = "ptrmchat"
  pcall(api.nvim_buf_set_name, chat, "ptrm://chat")
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
  local bufs = { chat = chat }
  local dash = api.nvim_create_buf(false, true)
  vim.bo[dash].bufhidden = "hide"
  vim.bo[dash].filetype = "ptrmdash"
  pcall(api.nvim_buf_set_name, dash, "ptrm://dash")
  api.nvim_buf_set_lines(dash, 0, -1, false, { " DASH — the table, live: waiting for the first look at the ports …" })
  vim.bo[dash].modifiable = false
  M._dash_keys(dash)
  api.nvim_create_autocmd("CursorMoved", { buffer = dash, callback = function()
    if S then S.dash_sel = dash_row() or S.dash_sel end
  end })
  bufs.dash = dash
  for key, ft in pairs({ log = "ptrmlog", iface = "ptrmiface" }) do
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].filetype = ft
    vim.bo[b].modifiable = false
    pcall(api.nvim_buf_set_name, b, "ptrm://" .. (key == "log" and "llm" or key))
    bufs[key] = b
    M._pane_keys(b)
    vim.keymap.set("n", "a", function() M.focus("chat") end, { buffer = b, silent = true, nowait = true })
  end
  local o = { buffer = chat, silent = true, nowait = true }
  vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
  vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
  vim.keymap.set("n", "q", function() M.back() end, o)
  vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
  vim.keymap.set("n", "i", function() M.focus("chat") end, o)
  return bufs
end

-- ---------------------------------------------------------------- lifecycle
local function normalize_args(args)
  args = args or vim.g.ptrm_args or {}
  if type(args) == "string" then args = vim.split(args, "%s+", { trimempty = true }) end
  return vim.deepcopy(args)
end

--- g:ptrm_stats entries: 'hosts' (the default: a monitor on each host of the table, once ptrm reaches it), 'local',
--- 'local:btop', 'user@host', 'user@host:nvtop', or {host=, tool=} (tool may be a whole command line:
--- {host='hostB', tool='watch -n 2 nvidia-smi'}) -> ops for the backend.
local function initial_stats()
  for _, spec in ipairs(vim.g.ptrm_stats or { "hosts" }) do
    local host, tool
    if type(spec) == "table" then
      host, tool = spec.host, spec.tool
    else
      host, tool = tostring(spec):match("^(.-):([%w%-_]+)$")
      host = host or tostring(spec)
    end
    send({ t = "op", op = "stats", host = (host and host ~= "local" and host ~= "") and host or nil, tool = tool })
  end
end

function M.start(args, opts)
  opts = opts or {}
  if S and S.job then return M.toggle() end
  local script = SCRIPT_DIR .. "/ptrm.py"
  args = normalize_args(args)
  local py = vim.g.ptrm_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", script, "--table", table_path() }
  if vim.g.ptrm_memory then vim.list_extend(cmd, { "--memory", native(fn.fnamemodify(fn.expand(vim.g.ptrm_memory), ":p")) }) end
  vim.list_extend(cmd, args)
  local old = S
  S = { bufs = old and old.bufs or create_buffers(), wins = old and old.wins or { stats = {} },
    stats = old and old.stats or {}, tab = old and old.tab, state = {},
    prev_tab = old and old.prev_tab or api.nvim_get_current_tabpage(), anchor = old and old.anchor or api.nvim_get_current_win() }
  if not in_tab() then S.prev_tab, S.anchor = api.nvim_get_current_tabpage(), api.nvim_get_current_win() end
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = LOAD_CWD, env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    return notify("cannot start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
  end
  if not old then initial_stats() end
  if opts.show ~= false then M.show() end
end

local function ensure_started()
  if not (S and S.job) then M.start(nil, { show = false }) end
  return S and S.job
end

function M.ask(text)
  if not (S and S.job) then
    M.start(nil, { show = false })
    vim.defer_fn(function() submit(text) end, 600)
    return
  end
  submit(text)
end

--- :PtrmCheck / :PtrmOpen / :PtrmClose / :PtrmReopen [port]: the port given, else the row under the cursor (in ptrm.md
--- or DASH), else ask.
function M.port_op(op, port, extra)
  local p = tonumber(port)
  if not p then
    if S and S.bufs and api.nvim_get_current_buf() == S.bufs.dash then
      local r = dash_row()
      p = r and r.port
    else
      p = row_port(api.nvim_get_current_line())
    end
  end
  local function go(v)
    v = tonumber(v)
    if not v then return end
    if ensure_started() then send(vim.tbl_extend("force", { t = "op", op = op, port = v }, extra or {})) end
  end
  if p then return go(p) end
  vim.ui.input({ prompt = "ptrm: " .. op .. " which local port? " }, go)
end

--- :PtrmDisableHost / :PtrmEnableHost [host]: the host given, else the one of the line under the cursor (DASH, or the
--- table's SSH_HOST cell; an empty cell: the host ptrm chose for the row), else ask.
function M.host_op(on, host)
  if not ensure_started() then return end
  host = vim.trim(host or "")
  local port
  if host == "" then
    if S and S.bufs and api.nvim_get_current_buf() == S.bufs.dash then
      local r = dash_row()
      host = r and r.host or ""
    else
      local line = api.nvim_get_current_line()
      host = vim.trim(line:match("^%s*|([^|]*)|") or "")
      if host:upper() == "SSH_HOST" or host:match("^[%-:%s]+$") then host = "" end
      port = row_port(line)
    end
  end
  if host ~= "" then return send({ t = "op", op = "disable_host", host = host, on = on }) end
  if port then return send({ t = "op", op = "disable_host", port = port, on = on }) end
  vim.ui.input({ prompt = "ptrm: " .. (on and "disable" or "enable") .. " which host? " }, function(v)
    if v and vim.trim(v) ~= "" then send({ t = "op", op = "disable_host", host = vim.trim(v), on = on }) end
  end)
end

--- :PtrmTerm [host]: a shell on the host in STATS; no host: the row under the cursor (DASH or the table), else ask.
function M.term(host)
  if not ensure_started() then return end
  host = vim.trim(host or "")
  if host ~= "" then return send({ t = "op", op = "term", host = host }) end
  local p
  if S and S.bufs and api.nvim_get_current_buf() == S.bufs.dash then
    local r = dash_row()
    if r and r.host and r.host ~= "" then return send({ t = "op", op = "term", host = r.host }) end
    p = r and r.port
  else
    p = row_port(api.nvim_get_current_line())
  end
  if p then return send({ t = "op", op = "term", port = p }) end
  vim.ui.input({ prompt = "ptrm: a terminal on which host? " }, function(v)
    if v and vim.trim(v) ~= "" then send({ t = "op", op = "term", host = vim.trim(v) }) end
  end)
end

function M.stats(host, tool)
  if ensure_started() then send({ t = "op", op = "stats", host = (host ~= "" and host ~= "local") and host or nil, tool = tool }) end
end

--- The table in your tab: the window already showing it, else a new tab (as VimPage_ptrm).
function M.edit_table()
  local path = (S and S.state and S.state.table and S.state.table ~= vim.NIL) and S.state.table or table_path()
  M.back()
  for _, w in ipairs(api.nvim_list_wins()) do
    if fn.fnamemodify(api.nvim_buf_get_name(api.nvim_win_get_buf(w)), ":p") == fn.fnamemodify(path, ":p") then
      api.nvim_set_current_tabpage(api.nvim_win_get_tabpage(w))
      return api.nvim_set_current_win(w)
    end
  end
  vim.cmd("tabnew " .. fn.fnameescape(path))
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  for _, st in ipairs(S.stats or {}) do
    if st.job then pcall(fn.jobstop, st.job) end
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) and #api.nvim_list_tabpages() > 1 then
    M.back()
    pcall(function() vim.cmd("tabclose " .. api.nvim_tabpage_get_number(S.tab)) end)
  end
  local bufs = vim.tbl_values(S.bufs)
  for _, st in ipairs(S.stats or {}) do bufs[#bufs + 1] = st.buf end
  for _, b in ipairs(bufs) do
    if valid_buf(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  S = nil
end

function M.status() return S and S.job and S.state.net or nil end
function M.running() return S ~= nil and S.job ~= nil end
M._session = function() return S end

local cmd = api.nvim_create_user_command
cmd("Ptrm", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start ptrm / go to its tab / back" })
cmd("PtrmToggle", function() M.toggle() end, { desc = "Go to the ptrm tab / back" })
cmd("PtrmAsk", function(o) M.ask(o.args) end, { nargs = "+", desc = "Ask ptrm" })
for _, name in ipairs({ "Check", "Open", "Close", "Reopen", "Bringup", "Teach" }) do
  cmd("Ptrm" .. name, function(o) M.port_op(name:lower(), o.args) end,
    { nargs = "?", desc = name .. " a port (the row under the cursor)" })
end
cmd("PtrmDisable", function(o) M.port_op("disable", o.args, { on = true }) end,
  { nargs = "?", desc = "Disable a row: ptrm leaves it alone (no bring-up, healing, incident)" })
cmd("PtrmEnable", function(o) M.port_op("disable", o.args, { on = false }) end,
  { nargs = "?", desc = "Enable a disabled row again (ptrm brings it up)" })
cmd("PtrmStats", function(o)
  local tool = #o.fargs > 1 and table.concat(vim.list_slice(o.fargs, 2), " ") or nil
  M.stats(o.fargs[1] or "", tool)
end, { nargs = "*", desc = "A monitor terminal in STATS: [host|local] [tool | a monitor command line]" })
cmd("PtrmDisableHost", function(o) M.host_op(true, o.args) end,
  { nargs = "?", desc = "Disable a host: ptrm stops using it (its rows left alone, never chosen)" })
cmd("PtrmEnableHost", function(o) M.host_op(false, o.args) end,
  { nargs = "?", desc = "Enable a disabled host again (its rows are brought up)" })
cmd("PtrmTerm", function(o) M.term(o.args) end,
  { nargs = "?", desc = "A shell on the host in STATS (the row under the cursor; 'local': this machine)" })
cmd("PtrmTable", function() M.edit_table() end, { desc = "Edit the ptrm table" })
cmd("PtrmCancel", function() send({ t = "cancel" }) end, {})
cmd("PtrmQuit", M.quit, {})

if vim.g.ptrm_toggle_key then
  vim.keymap.set({ "n", "i", "t" }, vim.g.ptrm_toggle_key, function()
    if fn.mode() == "t" or fn.mode() == "i" then vim.cmd("stopinsert") end
    M.toggle()
  end, { silent = true, desc = "ptrm tab: go / back" })
end

local aug = api.nvim_create_augroup("ptrm", { clear = true })
api.nvim_create_autocmd("VimResized", { group = aug, callback = function() if in_tab() then sizes() end end })
api.nvim_create_autocmd("TabEnter", { group = aug, callback = function() if in_tab() then layout() end end })
api.nvim_create_autocmd("TabClosed", { group = aug, callback = function()
  if S and S.tab and not api.nvim_tabpage_is_valid(S.tab) then
    S.tab, S.wins, S.grid = nil, { stats = {} }, {}
    S.stats_dirty = true
  end
end })
api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function() if S and S.job then send({ t = "quit" }) end end })
-- Leaving the input for another pane (mouse, <C-w>) leaves insert mode.
api.nvim_create_autocmd("WinEnter", { group = aug, callback = function()
  if S and in_tab() and api.nvim_get_current_win() ~= S.wins.chat and fn.mode() == "i" then vim.cmd("stopinsert") end
end })
api.nvim_create_autocmd("WinClosed", { group = aug, callback = function(ev)
  if not S then return end
  local w = tonumber(ev.match)
  for key, x in pairs(S.wins) do if x == w then S.wins[key] = nil end end
  for _, key in ipairs({ "stats", "terms" }) do
    for i, x in ipairs(S.wins[key] or {}) do
      if x == w then table.remove(S.wins[key], i); break end
    end
  end
end })

package.loaded.ptrm = M

-- ---------------------------------------------------------------- Intelligence Network
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("ptrm", {
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
    vim.notify("ptrm: Intelligence Network driver not loaded (" .. dir .. "/inet.lua): " .. tostring(inet),
      vim.log.levels.WARN)
  end
end

-- `:source ptrm.lua` (ptrm1) starts the agent and opens its tab, unless g:ptrm_no_autostart is set.
if MODNAME == nil and not vim.g.ptrm_no_autostart then M.start() end

return M
