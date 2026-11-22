-- knbg.lua: Neovim frontend of knbg, the Kanborg operator agent (requires Neovim >= 0.10).
-- Loaded only through ~/echo-home/.vim/mimd/vimpage_knbg; the kanborg tool itself knows nothing about it.
--
-- The agent (knbg.py, same folder) operates the kanborg tool on behalf of the user: project manager of the
-- board, Vim hands in this editor, coordinator on the Intelligence Network. It reaches the board only through
-- the tool's CLI. When the kanborg TUI module is loaded in this Neovim, its public Lua API (context / refresh /
-- view / open) is used, guarded by pcall; otherwise knbg shows views in its own scratch tab.
--
-- knbg lives in a framed float along the bottom of the editor, the editor showing in a margin left and right;
-- one pane per float inside the frame, side by side:
--     ╭───────────────────────────────┬───────────────────────────┬─────────────────────────╮
--     │ KNBG  ○ idle                  │ LLM  raw output           │ IFACE  🌐 @kanborg      │
--     │ what knbg does, your input on │ the raw LLM output,       │ the network: what knbg  │
--     │ the last line                 │ streamed as it is made    │ hears, decides, says    │
--     ╰───────────────────────────────┴───────────────────────────┴─────────────────────────╯
-- IFACE is there only while knbg is joined to the Intelligence Network. Each pane's header is a colored badge
-- and strip across the pane (as in hbmx); the KNBG header carries the status (busy/idle, queue, AUTO, approval).
--
-- `:source knbg.lua` (knbg1) starts knbg and opens the strip at once. Sourced again while knbg runs, it only shows
-- the strip; once knbg is stopped (:KnbgQuit), sourcing reloads this file fresh.
--
--   :Knbg [args]       start (backend args from g:knbg_args) / focus the strip / hide it when focused
--   :KnbgToggle        show / hide the strip (vimpage_knbg maps it to knk)
--   :KnbgAsk <text>    send a request without opening the strip
--   :KnbgCancel        cancel the running task            :KnbgQuit  stop the backend
--   :InetJoin knbg / :InetLeave knbg    (profile g:inet_agents.knbg in vimpage_inet)
--
-- Options: g:knbg_python ("python"), g:knbg_args (list), g:knbg_root (kanborg tool folder,
--          default ~/echo-home/codev/kanborg), g:knbg_no_autostart (sourcing only loads, :Knbg starts),
--          g:knbg_height (rows with the frame, or a fraction of the screen: 0.4), g:knbg_margin (2: columns of
--          editor left and right of the frame), g:knbg_llm_width (0.33) and g:knbg_iface_width (0.3): fractions of
--          the frame (KNBG takes the rest), g:knbg_toggle_key (e.g. '<M-k>'),
--          g:knbg_work_repos (list: git repositories whose commits are named after cards, e.g. ['~/echo-home']).
-- In the strip: last line of KNBG is the input · <CR> send · <C-c> cancel task · <Tab> (normal) next pane ·
--               <S-Tab> previous pane · i input · q hide (normal)

local MODNAME = ...

-- Sourced again: a running knbg keeps its session (only the strip is shown); a stopped one gives way to this load.
do
  local old = package.loaded.knbg
  if MODNAME == nil and type(old) == "table" then
    local alive = old.running or old.status
    if type(alive) == "function" and alive() then
      old.show(true)
      return old
    end
    if type(old.quit) == "function" then pcall(old.quit) end -- close its strip, drop its buffers
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
local NS = api.nvim_create_namespace("knbg")
local NS_FRAME = api.nvim_create_namespace("knbg_frame")
-- The input prompt " KanBorg ": " Kan" and "Borg " in inverted colors (KnbgPrompt / KnbgPromptInv).
local PROMPT_PARTS = { { " Kan", "KnbgPrompt" }, { "Borg ", "KnbgPromptInv" } }
local PROMPT = PROMPT_PARTS[1][1] .. PROMPT_PARTS[2][1]
local PANES = { "chat", "log", "iface" }                    -- the strip's panes, left to right (buffer keys)
local FLOATS = { "frame", "chat", "log", "iface" }          -- every float of the strip: the frame under the panes
local BADGE = { chat = "KNBG", log = "LLM", iface = "IFACE" }
local COLOR = { chat = "Main", log = "Llm", iface = "Iface" } -- Knbg<COLOR>Bar / Knbg<COLOR>Badge
local WIDTH_OPT = { log = "knbg_llm_width", iface = "knbg_iface_width" }
local WIDTH_DEFAULT = { log = 0.33, iface = 0.3 }
local S -- session; nil when not running

-- The prompt label: " Kan" mint on a deep forest green, "Borg " the inverse. The pane headers: a badge
-- and a strip per pane: KNBG mint on a deep forest green with a mint strip, LLM in gruvbox's blue (as in hbmx),
-- IFACE in its purple; the frame in gruvbox's bg4. The panes keep the colorscheme's background.
-- Override with  :hi KnbgPrompt ... / KnbgPromptInv / KnbgBorder / KnbgMainBar / KnbgMainBadge / KnbgLlmBar ...
-- (defined as defaults, re-applied after a colorscheme change).
local function set_hl()
  local d = function(name, spec) spec.default = true; api.nvim_set_hl(0, name, spec) end
  local light = vim.o.background == "light"
  d("KnbgPrompt", { bg = "#1b4332", fg = "#d8f3dc", bold = true, ctermbg = 22, ctermfg = 194, cterm = { bold = true } })
  d("KnbgPromptInv", { bg = "#d8f3dc", fg = "#1b4332", bold = true, ctermbg = 194, ctermfg = 22, cterm = { bold = true } })
  d("KnbgBorder", light and { fg = "#a89984", ctermfg = 246 } or { fg = "#7c6f64", ctermfg = 243 })
  -- bar: the strip (fg: its text, else the colorscheme's), badge / on: the badge and its text
  local MAIN = { bar = "#d8f3dc", fg = "#1b4332", badge = "#1b4332", on = "#d8f3dc", cbar = 194, cfg = 22, cb = 22, con = 194 }
  local P = light and {
    Main = MAIN,
    Llm = { bar = "#cadbea", badge = "#076678", on = "#fbf1c7", cbar = 153, cb = 24, con = 230 },
    Iface = { bar = "#e7cfdc", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cb = 96, con = 230 },
  } or {
    Main = MAIN,
    Llm = { bar = "#2a3b48", badge = "#83a598", on = "#1d2021", cbar = 17, cb = 109, con = 234 },
    Iface = { bar = "#43304a", badge = "#d3869b", on = "#1d2021", cbar = 53, cb = 175, con = 234 },
  }
  for name, p in pairs(P) do
    d("Knbg" .. name .. "Bar", { bg = p.bar, fg = p.fg, ctermbg = p.cbar, ctermfg = p.cfg })
    d("Knbg" .. name .. "Badge", { bg = p.badge, fg = p.on, bold = true, ctermbg = p.cb, ctermfg = p.con, cterm = { bold = true } })
  end
end
set_hl()
api.nvim_create_autocmd("ColorScheme", { callback = set_hl })
-- Painted at draw time on the chat buffer's input line, so it follows the prompt as the transcript grows.
local NS_PROMPT = api.nvim_create_namespace("knbg_prompt")
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

local function notify(msg, level) vim.notify("knbg: " .. msg, level or vim.log.levels.INFO) end

-- The board knbg works on: g:knbg_root if set, else the directory Neovim was in when knbg was loaded (knbg1),
-- i.e. the folder holding ./datum. :Knbg --root <dir> overrides both for one start.
local LOAD_CWD = fn.getcwd()

local function clean_dir(d)
  return (native(fn.fnamemodify(fn.expand(d), ":p")):gsub("[/\\]$", ""))
end

local function root()
  return clean_dir(vim.g.knbg_root or LOAD_CWD)
end

--- The kanborg CLI that runs the board: g:knbg_tool, else the board's own .function/kanborg.py,
--- else the reference tool in ~/echo-home/codev/kanborg.
local function tool_cli(board)
  if vim.g.knbg_tool then return native(fn.fnamemodify(fn.expand(vim.g.knbg_tool), ":p")) end
  local own = board .. "/.function/kanborg.py"
  if fn.filereadable(own) == 1 then return own end
  return native(fn.expand("~/echo-home/codev/kanborg/.function/kanborg.py"))
end

--- The kanborg TUI's public Lua API, only if the user runs the TUI in this Neovim (never loaded by knbg).
local function tui()
  local kb = package.loaded.kanborg
  return type(kb) == "table" and kb or nil
end

--- Call a TUI API function without letting a TUI error escape into the agent (or the other way round).
local function tui_call(name, ...)
  local kb = tui()
  if not (kb and type(kb[name]) == "function") then return false end
  local ok, res = pcall(kb[name], ...)
  return ok, res
end

local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function send(msg)
  if S and S.job then fn.chansend(S.job, vim.json.encode(msg) .. "\n") end
end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end

-- ---------------------------------------------------------------- buffers
--- Keep the panes that show this buffer (other than the one the user is in) at its end.
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
  if not (buf and api.nvim_buf_is_valid(buf)) then return end
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
--- Lines appended meanwhile (e.g. the network's attention call) go below it and are not split by the stream.
local function append_raw(key, text)
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
  parts[1] = (api.nvim_buf_get_lines(buf, S.raw_row, S.raw_row + 1, false)[1] or "") .. parts[1]
  api.nvim_buf_set_lines(buf, S.raw_row, S.raw_row + 1, false, parts)
  S.raw_row = S.raw_row + #parts - 1
  vim.bo[buf].modifiable = false
  scroll_to_end(buf)
end

-- ---------------------------------------------------------------- pane headers
local function net_state()
  local net = S and S.state and S.state.net
  return type(net) == "table" and net or {}
end

local function joined() return net_state().joined == true end

--- KNBG: the board and the agent's status.
local function main_status()
  local st = S and S.state or {}
  local parts = { S and S.board and fn.fnamemodify(S.board, ":t") or "kanborg" }
  parts[#parts + 1] = st.busy and ("● " .. tostring(st.phase or "busy")) or (S and S.job and "○ idle" or "✖ stopped")
  if (st.queue or 0) > 0 then parts[#parts + 1] = st.queue .. " queued" end
  if st.auto then parts[#parts + 1] = "AUTO" end
  if not joined() then parts[#parts + 1] = "🌐 off" end
  if st.approval and st.approval ~= vim.NIL then parts[#parts + 1] = "❓ y/n/a" end
  return table.concat(parts, " · ")
end

--- IFACE: who knbg is on which network.
local function net_status()
  local net = net_state()
  return "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
    .. " [" .. tostring(net.state) .. "]" .. ((net.members and net.connected) and (" · " .. net.members .. " members") or "")
end

--- LLM: the model the stream comes from.
local function llm_status()
  local model = (S and S.state or {}).model
  return "raw output, streamed" .. ((model and model ~= vim.NIL) and (" · " .. fn.fnamemodify(tostring(model), ":t")) or "")
end

local STATUS = { chat = main_status, log = llm_status, iface = net_status }

--- Cut text to a display width, marking the cut with … (Neovim would cut a too-long bar on the left: the badge).
local function fit(s, width)
  if fn.strdisplaywidth(s) <= width then return s end
  local n = fn.strchars(s)
  while n > 0 and fn.strdisplaywidth(fn.strcharpart(s, 0, n)) > width - 1 do n = n - 1 end
  return fn.strcharpart(s, 0, n) .. "…"
end

--- A pane's header: its badge in its color, then the status on its strip.
local function pane_bar(key, w)
  local c = COLOR[key]
  local room = api.nvim_win_get_width(w) - #BADGE[key] - 3
  return "%#Knbg" .. c .. "Badge# " .. BADGE[key] .. " %#Knbg" .. c .. "Bar# " .. esc(fit(STATUS[key](), math.max(1, room)))
end

local function refresh_bars()
  if not S then return end
  for _, key in ipairs(PANES) do
    local w = S.wins[key]
    if valid_win(w) then vim.wo[w].winbar = pane_bar(key, w) end
  end
end

-- ---------------------------------------------------------------- the strip
local function pane_of(win)
  if not (S and win) then return nil end
  for _, key in ipairs(PANES) do
    if S.wins[key] == win then return key end
  end
end

--- The strip is open in the current tab page (floats live in one tab page).
local function shown()
  return S ~= nil and valid_win(S.wins.chat) and api.nvim_win_get_tabpage(S.wins.chat) == api.nvim_get_current_tabpage()
end

--- The panes to show and the float configs: a frame at the bottom of the editor, above the status line, with a
--- margin of editor left and right (so it reads as a float); the panes inside it side by side, one frame column
--- between two panes. seps: the frame's columns (0 = its left side) that separate two panes.
local function geometry()
  local cols, lines = vim.o.columns, vim.o.lines
  local tabline = (vim.o.showtabline == 2 or (vim.o.showtabline == 1 and #api.nvim_list_tabpages() > 1)) and 1 or 0
  local bottom = lines - vim.o.cmdheight - (vim.o.laststatus == 0 and 0 or 1) -- the first row below the frame
  local h = tonumber(vim.g.knbg_height) or 0.4
  if h < 1 then h = math.floor((bottom - tabline) * h) end
  h = math.max(8, math.min(math.floor(h), bottom - tabline)) -- the frame's top and bottom rows included
  local margin = math.max(0, math.floor(tonumber(vim.g.knbg_margin) or 2))
  if cols - 2 * margin < 50 then margin = 0 end -- a narrow screen needs every column
  local outer = cols - 2 * margin -- the frame's width, its two sides included
  local list = { "chat", "log" }
  if joined() then list[#list + 1] = "iface" end
  local width
  for _ = 1, 2 do
    local usable = outer - (#list + 1) -- the frame's sides and one column between two panes
    width = { chat = usable }
    for _, key in ipairs(list) do
      if key ~= "chat" then
        width[key] = math.max(12, math.floor(usable * (tonumber(vim.g[WIDTH_OPT[key]]) or WIDTH_DEFAULT[key])))
        width.chat = width.chat - width[key]
      end
    end
    if width.chat >= 32 or list[2] ~= "log" then break end
    table.remove(list, 2) -- too narrow for three: the LLM stream gives way (shown again on a wider screen)
  end
  local top = bottom - h
  local cfg = { frame = { relative = "editor", row = top, col = margin, width = outer, height = h, border = "none",
    focusable = false, zindex = 59 } }
  local col, seps = margin + 1, {}
  for i, key in ipairs(list) do
    cfg[key] = { relative = "editor", row = top + 1, col = col, width = math.max(1, width[key]), height = h - 2,
      border = "none", zindex = 60 }
    col = col + width[key] + 1
    if i < #list then seps[col - 1 - margin] = true end
  end
  return list, cfg, seps
end

--- The frame: a rounded box, ┬ / │ / ┴ where two panes meet. It is the text of a buffer in a float under the panes.
local function draw_frame(c, seps)
  local buf = S.bufs.frame
  if not (buf and api.nvim_buf_is_valid(buf)) then
    buf = api.nvim_create_buf(false, true)
    vim.bo[buf].bufhidden = "hide"
    S.bufs.frame = buf
  end
  local top, mid, bot = { "╭" }, { "│" }, { "╰" }
  for i = 1, c.width - 2 do
    top[#top + 1] = seps[i] and "┬" or "─"
    mid[#mid + 1] = seps[i] and "│" or " "
    bot[#bot + 1] = seps[i] and "┴" or "─"
  end
  local lines = { table.concat(top) .. "╮" }
  for _ = 1, c.height - 2 do lines[#lines + 1] = table.concat(mid) .. "│" end
  lines[#lines + 1] = table.concat(bot) .. "╯"
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  api.nvim_buf_clear_namespace(buf, NS_FRAME, 0, -1)
  api.nvim_buf_set_extmark(buf, NS_FRAME, 0, 0, { end_row = #lines - 1, end_col = #lines[#lines], hl_group = "KnbgBorder" })
end

local function pane_opts(w, key)
  vim.wo[w].wrap = true
  vim.wo[w].number, vim.wo[w].relativenumber, vim.wo[w].cursorline, vim.wo[w].list = false, false, false, false
  vim.wo[w].signcolumn, vim.wo[w].foldcolumn = "no", "0"
  vim.wo[w].winhighlight = ("NormalFloat:Normal,WinBar:Knbg%s,WinBarNC:Knbg%s"):gsub("%%s", COLOR[key] .. "Bar")
  vim.wo[w].winbar = pane_bar(key, w)
end

local function close_float(key)
  local w = S.wins[key]
  S.wins[key] = nil
  if valid_win(w) then
    S.closing = true
    pcall(api.nvim_win_close, w, true)
    S.closing = false
  end
end

--- Open / move / resize the frame and the panes in the current tab page; IFACE follows the network (only while joined).
local function layout()
  local list, cfg, seps = geometry()
  local tab = api.nvim_get_current_tabpage()
  local in_pane = pane_of(api.nvim_get_current_win())
  for _, key in ipairs(FLOATS) do
    local w = S.wins[key]
    if not cfg[key] or (valid_win(w) and api.nvim_win_get_tabpage(w) ~= tab) then close_float(key) end
  end
  draw_frame(cfg.frame, seps)
  for _, key in ipairs(vim.list_extend({ "frame" }, list)) do
    local w = S.wins[key]
    if valid_win(w) then
      pcall(api.nvim_win_set_config, w, cfg[key])
    else
      w = api.nvim_open_win(S.bufs[key], false, vim.tbl_extend("force", cfg[key], { style = "minimal" }))
      S.wins[key] = w
      if key == "frame" then
        vim.wo[w].wrap = false
        vim.wo[w].winhighlight = "NormalFloat:Normal"
      else
        pane_opts(w, key)
        pcall(api.nvim_win_set_cursor, w, { api.nvim_buf_line_count(S.bufs[key]), 0 })
      end
    end
  end
  -- the pane you were in went away (IFACE after leaving the network): stay in the strip, on KNBG
  if in_pane and not valid_win(S.wins[in_pane]) and valid_win(S.wins.chat) then api.nvim_set_current_win(S.wins.chat) end
  refresh_bars()
end

--- Re-layout when the strip is on screen (state change, resize), else just keep the headers current.
local function refresh()
  if not S then return end
  if shown() then layout() else refresh_bars() end
end

--- Focus a pane of the strip; KNBG puts you on the input line, in insert mode.
function M.focus(key)
  if not (S and S.wins) then return end
  if not valid_win(S.wins[key]) then key = "chat" end
  local w = S.wins[key]
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
  if not S then return end
  local list = {}
  for _, key in ipairs(PANES) do
    if valid_win(S.wins[key]) then list[#list + 1] = key end
  end
  if #list == 0 then return end
  local i = 1
  for k, key in ipairs(list) do
    if key == pane_of(api.nvim_get_current_win()) then i = k end
  end
  M.focus(list[(i - 1 + delta) % #list + 1])
end

function M.show(enter)
  if not S then return M.start() end
  local cur = api.nvim_get_current_win()
  if not pane_of(cur) then S.anchor = cur end
  layout()
  if enter ~= false then M.focus("chat") end
end

function M.hide()
  if not (S and S.wins) then return end
  local inside = pane_of(api.nvim_get_current_win()) ~= nil
  if inside then vim.cmd("stopinsert") end
  for _, key in ipairs(FLOATS) do close_float(key) end
  if inside and valid_win(S.anchor) then pcall(api.nvim_set_current_win, S.anchor) end
end

--- :Knbg / knbgi: start, else focus the strip, else (already in it) hide it.
function M.toggle()
  if not S then return M.start() end
  if shown() and pane_of(api.nvim_get_current_win()) then return M.hide() end
  M.show(true)
end

--- :KnbgToggle / knk: show the strip (focused on the input) or hide it, wherever the cursor is.
function M.toggle_view()
  if not S then return M.start() end
  if shown() then return M.hide() end
  M.show(true)
end

-- ---------------------------------------------------------------- context & editor requests
local function context(win)
  win = valid_win(win) and win or api.nvim_get_current_win()
  local ok, ctx = tui_call("context", win)
  if ok and type(ctx) == "table" then return ctx end
  local buf = api.nvim_win_get_buf(win)
  local row = api.nvim_win_get_cursor(win)[1]
  local line = api.nvim_buf_get_lines(buf, row - 1, row, false)[1] or ""
  return { buffer = api.nvim_buf_get_name(buf), line = line, card = line:match("%u[%u%d]*%-%d+") }
end

local OPS = {}
function OPS.context() return context(S.anchor) end

function OPS.ex(p)
  local win = valid_win(S.anchor) and S.anchor or api.nvim_get_current_win()
  return api.nvim_win_call(win, function() return api.nvim_exec2(p.command, { output = true }).output end)
end

function OPS.lua(p)
  local chunk, err = load(p.code, "kanborg-agent", "t")
  if not chunk then error(err) end
  local printed = {}
  local orig = print
  print = function(...) printed[#printed + 1] = table.concat(vim.tbl_map(tostring, { ... }), " ") end
  local win = valid_win(S.anchor) and S.anchor or api.nvim_get_current_win()
  local ok, res = pcall(api.nvim_win_call, win, chunk)
  print = orig
  if not ok then error(res) end
  local out = table.concat(printed, "\n")
  if res ~= nil then out = out .. (out ~= "" and "\n" or "") .. vim.inspect(res) end
  return out
end

function OPS.refresh()
  tui_call("refresh")
  return "ok"
end

--- After a view/card opened in another tab page: bring the strip along if it was on screen.
local function reshow(was_shown)
  if S and was_shown then M.show(false) end
end

--- knbg's own viewer for when the kanborg TUI is not running here: the CLI text in a scratch tab.
local function scratch_view(title, text)
  local buf
  for _, b in ipairs(api.nvim_list_bufs()) do
    if vim.b[b].knbg_view then buf = b end
  end
  local win
  for _, w in ipairs(api.nvim_list_wins()) do
    if buf and api.nvim_win_get_buf(w) == buf then win = w end
  end
  if win then
    api.nvim_set_current_win(win)
  else
    vim.cmd("tabnew")
    if buf then
      api.nvim_win_set_buf(0, buf)
    else
      buf = api.nvim_get_current_buf()
      vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile = "nofile", "hide", false
      vim.b[buf].knbg_view = true
      vim.keymap.set("n", "q", "<cmd>tabclose<cr>", { buffer = buf, nowait = true })
    end
    vim.wo.wrap = false
  end
  pcall(api.nvim_buf_set_name, buf, "knbg://" .. title)
  vim.bo[buf].modifiable = true
  api.nvim_buf_set_lines(buf, 0, -1, false, vim.split(text or "", "\n", { plain = true }))
  vim.bo[buf].modifiable = false
end

function OPS.view(p)
  local was_shown = shown()
  if valid_win(S.anchor) then api.nvim_set_current_win(S.anchor) end
  local ok = tui_call("view", p.kind, p.codes or {})
  if not ok then scratch_view(p.kind .. (#(p.codes or {}) > 0 and (" " .. table.concat(p.codes, ",")) or ""), p.text) end
  S.anchor = api.nvim_get_current_win()
  reshow(was_shown)
  return ok and "shown in the kanborg TUI" or "shown in a knbg tab"
end

function OPS.open(p)
  local was_shown = shown()
  if not tui_call("open", p.card) then
    if not p.path or p.path == "" then error("no path for " .. tostring(p.card)) end
    vim.cmd("tabnew " .. fn.fnameescape(p.path) .. " | setlocal filetype=markdown")
  end
  S.anchor = api.nvim_get_current_win()
  reshow(was_shown)
  return "ok"
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
      append_raw(b, msg.text)
    else
      if msg.eos then S.raw_row = nil end -- the stream ended: the next one starts on a fresh line
      append(b, msg.text, msg.hl ~= vim.NIL and msg.hl or nil)
    end
  elseif msg.t == "state" then
    local was = joined()
    S.state = msg
    if joined() ~= was then refresh() else refresh_bars() end -- joining opens IFACE, leaving closes it
  elseif msg.t == "req" then
    handle_req(msg)
  elseif msg.t == "show" then
    M.show(true)
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
  append("chat", "knbg stopped (exit " .. tostring(code) .. "). :Knbg restarts it.",
    code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh()
end

-- ---------------------------------------------------------------- input
local function submit(text, ctx)
  text = vim.trim(text or "")
  if text == "" then return end
  if not (S and S.job) then return notify("not running (:Knbg)", vim.log.levels.WARN) end
  send({ t = "input", text = text, ctx = ctx or context(S.anchor) })
end

local function create_buffers()
  local chat = api.nvim_create_buf(false, true)
  vim.bo[chat].buftype = "prompt"
  vim.bo[chat].bufhidden = "hide"
  vim.bo[chat].filetype = "knbgchat"
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
  for key, ft in pairs({ log = "knbglog", iface = "knbgiface" }) do
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].filetype = ft
    vim.bo[b].modifiable = false
    pcall(api.nvim_buf_set_name, b, "knbg://" .. (key == "log" and "llm" or key))
    bufs[key] = b
  end
  for _, b in pairs(bufs) do
    local o = { buffer = b, silent = true, nowait = true }
    vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
    vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
    vim.keymap.set("n", "q", M.hide, o)
    vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
    vim.keymap.set("n", "i", function() M.focus("chat") end, o)
    if b ~= chat then vim.keymap.set("n", "a", function() M.focus("chat") end, o) end
  end
  return bufs
end

-- ---------------------------------------------------------------- lifecycle
local function normalize_args(args)
  args = args or vim.g.knbg_args or {}
  if type(args) == "string" then args = vim.split(args, "%s+", { trimempty = true }) end
  return vim.deepcopy(args)
end

function M.start(args)
  if S and S.job then return M.toggle() end
  local script = SCRIPT_DIR .. "/knbg.py"
  args = normalize_args(args)
  local board = root()
  for i, a in ipairs(args) do
    if a == "--root" and args[i + 1] then board = clean_dir(args[i + 1]) end
  end
  -- On Windows "python3" is often the Microsoft Store stub; prefer "python" there (as memphia does).
  local py = vim.g.knbg_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", script }
  vim.list_extend(cmd, { "--root", board })
  if not vim.tbl_contains(args, "--tool") then vim.list_extend(cmd, { "--tool", tool_cli(board) }) end
  for _, repo in ipairs(vim.g.knbg_work_repos or {}) do -- where the commits named after cards are (board_history work)
    vim.list_extend(cmd, { "--work-repo", clean_dir(repo) })
  end
  for i = 1, #args do -- --root already resolved above
    if not (args[i] == "--root" or args[i - 1] == "--root") then cmd[#cmd + 1] = args[i] end
  end
  local old = S
  S = { bufs = old and old.bufs or create_buffers(), wins = old and old.wins or {}, state = {}, board = board,
    anchor = old and old.anchor or api.nvim_get_current_win() }
  if not pane_of(api.nvim_get_current_win()) then S.anchor = api.nvim_get_current_win() end
  -- tell the backend where the inet driver lives, as a clean native path (Git Bash: /c/.. -> C:/..)
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = board, env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    return notify("cannot start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
  end
  M.show(true)
end

function M.ask(text)
  if not (S and S.job) then
    M.start()
    vim.defer_fn(function() submit(text) end, 600)
    return
  end
  submit(text)
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  M.hide()
  for _, b in pairs(S.bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  S = nil
end

function M.status() return S and S.job and S.state.net or nil end
function M.running() return S ~= nil and S.job ~= nil end

local cmd = api.nvim_create_user_command
cmd("Knbg", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start / focus / hide knbg, the Kanborg operator" })
cmd("KnbgToggle", function() M.toggle_view() end, { desc = "Show / hide the knbg strip" })
cmd("KnbgAsk", function(o) M.ask(o.args) end, { nargs = "+", desc = "Ask knbg" })
cmd("KnbgCancel", function() send({ t = "cancel" }) end, {})
cmd("KnbgQuit", M.quit, {})

if vim.g.knbg_toggle_key then
  vim.keymap.set({ "n", "i", "t" }, vim.g.knbg_toggle_key, function()
    if fn.mode() == "t" or fn.mode() == "i" then vim.cmd("stopinsert") end
    M.toggle_view()
  end, { silent = true, desc = "knbg strip: show / hide" })
end

local aug = api.nvim_create_augroup("knbg", { clear = true })
api.nvim_create_autocmd("VimResized", { group = aug, callback = refresh })
api.nvim_create_autocmd("TabEnter", { group = aug, callback = refresh }) -- IFACE may have come or gone meanwhile
api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function() if S and S.job then send({ t = "quit" }) end end })
-- Leaving the input for another pane (mouse, <C-w>) leaves insert mode: LLM and IFACE are read-only.
api.nvim_create_autocmd("WinEnter", { group = aug, callback = function()
  local key = pane_of(api.nvim_get_current_win())
  if key and key ~= "chat" and fn.mode() == "i" then vim.cmd("stopinsert") end
end })
-- A pane (or the frame) closed by hand (:q, <C-w>c, :only) closes the whole strip; knk / :Knbg brings it back.
api.nvim_create_autocmd("WinClosed", { group = aug, callback = function(ev)
  if not (S and S.wins) or S.closing then return end
  local win = tonumber(ev.match)
  for _, key in ipairs(FLOATS) do
    if S.wins[key] == win then
      S.wins[key] = nil
      return vim.schedule(M.hide)
    end
  end
end })

package.loaded.knbg = M

-- ---------------------------------------------------------------- Intelligence Network
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("knbg", {
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
    vim.notify("knbg: Intelligence Network driver not loaded (" .. dir .. "/inet.lua): " .. tostring(inet),
      vim.log.levels.WARN)
  end
end

-- `:source knbg.lua` (knbg1) starts the agent and opens the strip, unless g:knbg_no_autostart is set.
if MODNAME == nil and not vim.g.knbg_no_autostart then M.start() end

return M
