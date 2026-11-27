-- gml8.lua: Neovim frontend of gml8, the operator of this machine's llama.cpp LLM services (requires Neovim >= 0.10).
-- Loaded through ~/echo-home/.vim/mimd/gml8 (the vimpage: gml81 · gml8k; gml8e stays the plain inventory tab).
--
-- The agent (gml8.py, same folder; engine gml8_core.py) keeps the inventory gml.<hostname>.yson in step with the
-- series (merge at start), launches its entries as detached llama-servers (they outlive gml8 and Neovim: gml8q leaves
-- them running), tracks every run through the OpenAI API (/v1/models), reads their logs, diagnoses and heals them,
-- relaunches an entry with changed params, publishes the services on the discovery bus (gml8bus.lua) for the agent
-- shells (mksk …), and powers its own LLM from the first service that answers.
--
-- gml8 has its own tab page:
--     ┌─ INVENTORY gml.<host>.yson ⚡1 ───────┬─ MODEL IDX #11 · gemma-4-E2B… ─────────────────┐
--     │  1 model: ["GPU/", "…"]    PORT 8090  │ the entry under the cursor, in depth: the GGUF,  │  top: the
--     │  2 model: []             ◌ placeholder│ the params by kind, the build, capacity, run …   │  inventory
--     │ …  (line numbers: reserved = slots)   ├─ STAGE #11 · edit (only after ss) ─────────────┤  (the file
--     │ 28 ---- NAMED ----                    │ the entry as fields: edit, IDX follows, C commits │  itself) and
--     │ 29 model: ["GPU/name", …]             ├─ LOG #11 :8090 · live ──────────────────────────┤  the model
--     │                                       │ tail -F of the server's log (running, or failed)  │
--     ├─ GML8 ○ idle · ⚡ #11 :8090 ─┬─ LLM ──┴────────────────────┬─ IFACE ──────────────────────┤
--     │ actions, chat; input last    │ gml8's own LLM output       │ the network (while joined)   │
--     └──────────────────────────────┴─────────────────────────────┴──────────────────────────────┘
--
--   :Gml8 [args]          start (backend args from g:gml8_args) / go to the gml8 tab / back to your tab
--   :Gml8Toggle           the same (gml8k)
--   :Gml8Launch [ref]     launch the entry under the cursor (the inventory, MODEL IDX) or ref (11, #11, @name)  (gml8w)
--   :Gml8Relaunch [ref]   stop it and launch it again, with its STAGE when that differs from the entry (gml8r)
--   :Gml8Stop [ref|port]  · :Gml8Commit [ref] write the STAGE into the entry's line (gml8c) · :Gml8Merge
--   :Gml8Ask <text>       a request without going to the tab · :Gml8Log  the LOG pane on/off
--   :Gml8Cancel           cancel the running task · :Gml8Quit  stop gml8 — the services keep running (gml8q)
--   :Gml8Services         the services on the bus · :InetJoin gml8 / :InetLeave gml8
--
-- Options: g:gml8_python, g:gml8_args (list), g:gml8_inventory (default g:GML_PROFILE, else
--          ~/echo-home/workstation/lmstudio/gmlama/.properd/gml.<hostname>.yson), g:gml8_memory (~/echo-home/memory/gml8),
--          g:gml8_inv_width (0.5: INVENTORY's share of the top row), g:gml8_bottom_height (rows or a fraction; 34%),
--          g:gml8_llm_width (0.34), g:gml8_iface_width (0.26), g:gml8_log_height (0.42 of the right column),
--          g:gml8_toggle_key (e.g. '<M-g>'), g:gml8_no_autostart.
-- In the tab: <Tab>/<S-Tab> next/previous pane · q back to your tab (not in INVENTORY) · i the input · <C-c> cancel.
-- INVENTORY: the cursor picks the entry (unsaved edits of its line show in MODEL IDX) · <CR> MODEL IDX.
-- MODEL IDX: L launch · R relaunch · x stop · ss show the STAGE and edit it · C commit the STAGE · u reset it · e the
--            entry's line · l the LOG pane on/off · y yank the command · a ask about the entry · r refresh · m merge.
-- STAGE:     shown only after ss · edit freely (MODEL IDX re-measures as you type) · <CR> (normal) back to MODEL IDX ·
--            q closes it (what is staged stays staged: C / L / u from MODEL IDX).
-- INVENTORY's right edge, as chips: PORT 8090 only on the entry gml8 launched, once it is up (Gml8EdgeUp; the port
--            in the same colours inverted: Gml8EdgeUpInv) — not on another entry with the same port, not for a server
--            started outside gml8 · FILE 404, faint and bold: the model file is not on this machine (Gml8EdgeMissing;
--            404 in its colours inverted: Gml8Edge404) · 💥 crashed … · ≠ series #N · section lines: Gml8Section.

local MODNAME = ...

do -- sourced again: a running gml8 keeps its session (only its tab is shown); a stopped one gives way to this load
  local old = package.loaded.gml8
  if MODNAME == nil and type(old) == "table" then
    if type(old.running) == "function" and old.running() then
      if not vim.g.gml8_no_autostart then old.show() end
      return old
    end
    if type(old.quit) == "function" then pcall(old.quit) end
  end
end

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
local NS = api.nvim_create_namespace("gml8")
local NS_INV = api.nvim_create_namespace("gml8_inv")
local NS_IDX = api.nvim_create_namespace("gml8_idx")
local NS_PROMPT = api.nvim_create_namespace("gml8_prompt")
local PROMPT_CHIP = " GML8 " -- Gml8Prompt, then a space before the input
local PROMPT = PROMPT_CHIP .. " "
local BOTTOM = { "chat", "log", "iface" }
local BADGE = { chat = "GML8", log = "LLM", iface = "IFACE" }
local COLOR = { chat = "Main", log = "Llm", iface = "Iface" }
local S -- the session; nil when not running

local bus = package.loaded.gml8bus
if not bus then
  local ok, b = pcall(dofile, SCRIPT_DIR .. "/gml8bus.lua")
  bus = ok and b or nil
end

-- ---------------------------------------------------------------- highlights (gruvbox-ish, light and dark)
local function set_hl()
  local d = function(name, spec) spec.default = true; api.nvim_set_hl(0, name, spec) end
  local light = vim.o.background == "light"
  local P = light and {
    Main = { bar = "#e6e9c0", fg = "#3c3a0a", badge = "#79740e", on = "#fbf1c7", cbar = 230, cfg = 58, cb = 100, con = 230,
      mid = "#baba79", mfg = "#2a2806", cmid = 144, cmfg = 58 },
    Llm = { bar = "#cadbea", fg = "#0b2e3a", badge = "#076678", on = "#fbf1c7", cbar = 153, cfg = 23, cb = 24, con = 230,
      mid = "#7cacbc", mfg = "#08242e", cmid = 109, cmfg = 23 },
    Iface = { bar = "#e7cfdc", fg = "#3a1530", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cfg = 53, cb = 96, con = 230 },
    Inv = { bar = "#d5e6d3", fg = "#1d3b2a", badge = "#427b58", on = "#fbf1c7", cbar = 194, cfg = 22, cb = 29, con = 230,
      mid = "#9cc3a4", mfg = "#10261a", cmid = 108, cmfg = 22 },
    Idx = { bar = "#f6dcc4", fg = "#4a2205", badge = "#af3a03", on = "#fbf1c7", cbar = 223, cfg = 52, cb = 130, con = 230,
      mid = "#da9b76", mfg = "#3a1a04", cmid = 173, cmfg = 52 },
    Stage = { bar = "#e9d8ef", fg = "#3c1361", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cfg = 54, cb = 96, con = 230 },
    Tail = { bar = "#f2e5bc", fg = "#5a4209", badge = "#b57614", on = "#fbf1c7", cbar = 230, cfg = 94, cb = 136, con = 230,
      mid = "#dab979", mfg = "#3d2c05", cmid = 180, cmfg = 58 },
    TailOff = { bar = "#ebdbb2", fg = "#7c6f64", badge = "#928374", on = "#fbf1c7", cbar = 223, cfg = 243, cb = 245, con = 230,
      mid = "#c7b899", mfg = "#4a4038", cmid = 144, cmfg = 238 },
  } or {
    Main = { bar = "#32361a", fg = "#e0e8b0", badge = "#b8bb26", on = "#1d2021", cbar = 58, cfg = 187, cb = 142, con = 234,
      mid = "#5a5e1e", mfg = "#f0f4c8", cmid = 58, cmfg = 230 },
    Llm = { bar = "#2a3b48", fg = "#d5e4ee", badge = "#83a598", on = "#1d2021", cbar = 17, cfg = 189, cb = 109, con = 234,
      mid = "#455b60", mfg = "#e4eff6", cmid = 66, cmfg = 195 },
    Iface = { bar = "#43304a", fg = "#f0d8e8", badge = "#d3869b", on = "#1d2021", cbar = 53, cfg = 225, cb = 175, con = 234 },
    Inv = { bar = "#263b30", fg = "#cfe8d0", badge = "#8ec07c", on = "#1d2021", cbar = 22, cfg = 194, cb = 108, con = 234,
      mid = "#456b52", mfg = "#e6f4e2", cmid = 65, cmfg = 194 },
    Idx = { bar = "#45301c", fg = "#f6dcc4", badge = "#fe8019", on = "#1d2021", cbar = 94, cfg = 223, cb = 208, con = 234,
      mid = "#7c481b", mfg = "#fde8d4", cmid = 130, cmfg = 230 },
    Stage = { bar = "#3b2840", fg = "#ecd5f2", badge = "#c4a7e7", on = "#1d2021", cbar = 54, cfg = 225, cb = 183, con = 234 },
    Tail = { bar = "#3c3418", fg = "#f2e5bc", badge = "#fabd2f", on = "#1d2021", cbar = 58, cfg = 230, cb = 214, con = 234,
      mid = "#755d1f", mfg = "#fbf0d0", cmid = 94, cmfg = 230 },
    TailOff = { bar = "#32302f", fg = "#a89984", badge = "#7c6f64", on = "#1d2021", cbar = 236, cfg = 246, cb = 243, con = 234,
      mid = "#48433f", mfg = "#d5c4a1", cmid = 238, cmfg = 187 },
  }
  -- a header: Badge, then chips — Mid (a shade between the badge and the bar), Dark (inverted colours on a dark
  -- background: the bar's in a light theme, the badge's in a dark one) — then the Bar to the window's edge
  local function dark(p)
    if light then return { bg = p.fg, fg = p.bar, bold = true, ctermbg = p.cfg, ctermfg = p.cbar, cterm = { bold = true } } end
    return { bg = p.on, fg = p.badge, bold = true, ctermbg = p.con, ctermfg = p.cb, cterm = { bold = true } }
  end
  for name, p in pairs(P) do
    d("Gml8" .. name .. "Bar", { bg = p.bar, fg = p.fg, ctermbg = p.cbar, ctermfg = p.cfg })
    d("Gml8" .. name .. "Badge", { bg = p.badge, fg = p.on, bold = true, ctermbg = p.cb, ctermfg = p.con, cterm = { bold = true } })
    d("Gml8" .. name .. "Dark", dark(p))
    if p.mid then
      d("Gml8" .. name .. "Mid", { bg = p.mid, fg = p.mfg, bold = true, ctermbg = p.cmid, ctermfg = p.cmfg, cterm = { bold = true } })
    end
  end
  -- the prompt at GML8's input line: one chip, GML8's Dark
  d("Gml8Prompt", dark(P.Main))
  local C = light and { orange = "#af3a03", grey = "#7c6f64", fg = "#3c3836", aqua = "#427b58", yellow = "#b57614",
    green = "#79740e", red = "#9d0006", blue = "#076678", purple = "#8f3f71", chipfg = "#fbf1c7", sel = "#ebdbb2" }
    or { orange = "#fe8019", grey = "#a89984", fg = "#ebdbb2", aqua = "#8ec07c", yellow = "#fabd2f", green = "#b8bb26",
      red = "#fb4934", blue = "#83a598", purple = "#d3869b", chipfg = "#1d2021", sel = "#3c3836" }
  d("Gml8Head", { fg = C.orange, bold = true, ctermfg = 208, cterm = { bold = true } })
  d("Gml8Label", { fg = C.grey, bold = true, ctermfg = 246, cterm = { bold = true } })
  d("Gml8Val", { fg = C.fg, bold = true, ctermfg = light and 237 or 223, cterm = { bold = true } })
  d("Gml8Flag", { fg = C.aqua, bold = true, ctermfg = 108, cterm = { bold = true } })
  d("Gml8Arg", { fg = C.yellow, ctermfg = 214 })
  d("Gml8Ok", { fg = C.green, bold = true, ctermfg = 142, cterm = { bold = true } })
  d("Gml8Warn", { fg = C.orange, bold = true, ctermfg = 208, cterm = { bold = true } })
  d("Gml8Bad", { fg = C.red, bold = true, ctermfg = 167, cterm = { bold = true } })
  d("Gml8Dim", { link = "Comment" })
  d("Gml8Model", { fg = C.blue, bold = true, ctermfg = 109, cterm = { bold = true } })
  d("Gml8Port", { fg = C.green, bold = true, ctermfg = 142, cterm = { bold = true } })
  d("Gml8Cmd", { fg = C.grey, italic = true, ctermfg = 246, cterm = { italic = true } })
  d("Gml8Staged", { fg = C.purple, bold = true, ctermfg = 175, cterm = { bold = true } })
  d("Gml8KeyChip", { bg = C.orange, fg = C.chipfg, bold = true, ctermbg = 208, ctermfg = 234, cterm = { bold = true } })
  d("Gml8ProfileChip", { bg = C.sel, fg = C.yellow, bold = true, ctermbg = light and 223 or 237, ctermfg = 214, cterm = { bold = true } })
  for name, col in pairs({ Up = C.green, Loading = C.yellow, Warn = C.orange, Bad = C.red, Dim = C.grey }) do
    d("Gml8" .. name .. "Chip", { bg = col, fg = C.chipfg, bold = true, cterm = { bold = true } })
  end
  d("Gml8Up", { fg = C.green, bold = true, ctermfg = 142, cterm = { bold = true } })
  d("Gml8Loading", { fg = C.yellow, bold = true, ctermfg = 214, cterm = { bold = true } })
  d("Gml8Missing", { fg = C.red, italic = true, ctermfg = 167, cterm = { italic = true } })
  d("Gml8Placeholder", { fg = C.grey, italic = true, ctermfg = 245, cterm = { italic = true } })
  d("Gml8Section", { fg = C.orange, bold = true, ctermfg = 208, cterm = { bold = true } })
  -- the states at INVENTORY's right edge: a dark background and a bright text, one colour per state (any theme);
  -- the value after a label (the port in PORT 8090) in the same colours inverted: Gml8Edge<state>Inv
  for name, c in pairs({
    Up = { "#1f3d1f", "#b8f56a", 22, 154 }, Loading = { "#3d3510", "#ffd75f", 58, 221 },
    Warn = { "#4a2a0a", "#ffaf5f", 94, 215 }, Bad = { "#4a1414", "#ff6b6b", 52, 203 },
    Foreign = { "#102f45", "#7fd4ff", 24, 117 }, Dim = { "#303030", "#e8e8e8", 236, 255 },
  }) do
    d("Gml8Edge" .. name, { bg = c[1], fg = c[2], bold = true, ctermbg = c[3], ctermfg = c[4], cterm = { bold = true } })
    d("Gml8Edge" .. name .. "Inv", { bg = c[2], fg = c[1], bold = true, ctermbg = c[4], ctermfg = c[3], cterm = { bold = true } })
  end
  -- FILE 404 (the model file is not on this machine): a faint magenta label, 404 in the same colours inverted; bold
  d("Gml8EdgeMissing", { bg = "#2b1a2b", fg = "#b48ab4", bold = true, ctermbg = 236, ctermfg = 139, cterm = { bold = true } })
  d("Gml8Edge404", { bg = "#b48ab4", fg = "#2b1a2b", bold = true, ctermbg = 139, ctermfg = 236, cterm = { bold = true } })
  d("Gml8Think", { fg = light and "#8a8a7a" or "#7c7a70", italic = true, ctermfg = 244, cterm = { italic = true } })
end
set_hl()
api.nvim_create_autocmd("ColorScheme", { callback = set_hl })

api.nvim_set_decoration_provider(NS_PROMPT, {
  on_win = function(_, _, buf) return S ~= nil and S.bufs ~= nil and buf == S.bufs.chat end,
  on_line = function(_, _, buf, row)
    if row ~= api.nvim_buf_line_count(buf) - 1 then return end
    local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    if line:sub(1, #PROMPT) == PROMPT then
      api.nvim_buf_set_extmark(buf, NS_PROMPT, row, 0, { end_col = #PROMPT_CHIP, hl_group = "Gml8Prompt", ephemeral = true, priority = 200 })
    end
  end,
})

local function notify(msg, level) vim.notify("gml8: " .. msg, level or vim.log.levels.INFO) end
local LOAD_CWD = fn.getcwd()
local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function valid_buf(b) return b and api.nvim_buf_is_valid(b) end
local function send(msg)
  if S and S.job then fn.chansend(S.job, vim.json.encode(msg) .. "\n") end
end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end
local function in_tab() return S ~= nil and S.tab ~= nil and api.nvim_tabpage_is_valid(S.tab) and api.nvim_get_current_tabpage() == S.tab end
local function win_in_tab(w) return valid_win(w) and S.tab ~= nil and api.nvim_win_get_tabpage(w) == S.tab end
local function nn(v) return v ~= nil and v ~= vim.NIL end

--- The inventory, as the vimpage names it: g:gml8_inventory, else g:GML_PROFILE, else gml.<hostname>.yson.
local function inventory_path()
  local p = vim.g.gml8_inventory or vim.g.GML_PROFILE
  if not p or p == "" then
    local host = vim.g.hostname or vim.trim(fn.system("hostname"))
    p = "~/echo-home/workstation/lmstudio/gmlama/.properd/gml." .. host .. ".yson"
  end
  return native(fn.fnamemodify(fn.expand(p), ":p"))
end

local function is_inv(buf)
  return S and valid_buf(buf) and fn.fnamemodify(api.nvim_buf_get_name(buf), ":p") == S.inv_path
end

local function inv_buf()
  if S and valid_buf(S.inv_buf) and is_inv(S.inv_buf) then return S.inv_buf end
  local b = fn.bufadd(S.inv_path)
  fn.bufload(b)
  vim.bo[b].buflisted = true
  if vim.bo[b].filetype == "" then vim.bo[b].filetype = "yaml" end
  vim.bo[b].autoread = true
  S.inv_buf = b
  M._inv_keys(b)
  return b
end

-- ---------------------------------------------------------------- pane text
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
    at = api.nvim_buf_line_count(buf) - 1
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
    for i = 0, #lines - 1 do pcall(api.nvim_buf_set_extmark, buf, NS, at + i, 0, { line_hl_group = hl, priority = 50 }) end
  end
  scroll_to_end(buf)
end

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
  api.nvim_buf_set_text(buf, row0, col0, row0, col0, parts)
  S.raw_row = row0 + #parts - 1
  if hl then
    local end_col = #parts == 1 and col0 + #parts[1] or #parts[#parts]
    local last = S.raw_mark
    if last and last.buf == buf and last.hl == hl and last.erow == row0 and last.ecol == col0 then
      pcall(api.nvim_buf_set_extmark, buf, NS, last.row, last.col, { id = last.id, end_row = S.raw_row, end_col = end_col, hl_group = hl, priority = 60 })
    else
      local ok, id = pcall(api.nvim_buf_set_extmark, buf, NS, row0, col0, { end_row = S.raw_row, end_col = end_col, hl_group = hl, priority = 60 })
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


local function power_text()
  local p = S and S.state and S.state.power
  if type(p) == "table" and p.external then -- ===Lf: a service gml8 does not run
    return "⚡ ext " .. (p.host ~= "127.0.0.1" and tostring(p.host) or "") .. ":" .. tostring(p.port)
  end
  if type(p) == "table" then return "⚡ " .. tostring(p.key) .. " :" .. tostring(p.port) end
  return "⚡ unpowered"
end

-- GML8's and LLM's headers as chips: {text, "Mid" | "Dark" | "Bar", shrinks}; the Bar runs on to the window's edge
local function main_chips()
  local st = S and S.state or {}
  local rest = {}
  if (st.queue or 0) > 0 then rest[#rest + 1] = st.queue .. " queued" end
  if st.auto then rest[#rest + 1] = "AUTO" end
  if not joined() then rest[#rest + 1] = "🌐 off" end
  if nn(st.approval) then rest[#rest + 1] = "❓ y/n/a" end
  return {
    { st.busy and ("● " .. tostring(st.phase or "busy")) or (S and S.job and "○ idle" or "✖ stopped"), "Mid" },
    { power_text(), "Dark" },
    { table.concat(rest, " · "), "Bar", true },
  }
end

local function llm_chips()
  local st = S and S.state or {}
  local p = st.power
  if type(p) ~= "table" then return { { "waiting for a service to power it", "Mid", true } } end
  return {
    { fn.fnamemodify(tostring(p.model), ":t"), "Mid", true },
    { power_text(), "Dark" },
    { st.think and "💭 thinking" or "", "Bar" },
  }
end

local function net_status()
  local net = net_state()
  return "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
    .. " [" .. tostring(net.state) .. "]" .. ((net.members and net.connected) and (" · " .. net.members .. " members") or "")
end

local CHIPS = { chat = main_chips, log = llm_chips }

local function fit(s, width)
  if fn.strdisplaywidth(s) <= width then return s end
  local n = fn.strchars(s)
  while n > 0 and fn.strdisplaywidth(fn.strcharpart(s, 0, n)) > width - 1 do n = n - 1 end
  return fn.strcharpart(s, 0, n) .. "…"
end

local function bar(color, badge, text, w)
  local room = api.nvim_win_get_width(w) - fn.strdisplaywidth(badge) - 3
  return "%#Gml8" .. color .. "Badge# " .. esc(badge) .. " %#Gml8" .. color .. "Bar# " .. esc(fit(text, math.max(1, room)))
end

--- A header of chips after the badge (see main_chips); the part that shrinks takes what the others leave.
local function chips(color, badge, parts, w)
  local head = " " .. badge .. " "
  local fixed = fn.strdisplaywidth(head)
  for _, p in ipairs(parts) do
    if p[1] ~= "" and not p[3] then fixed = fixed + fn.strdisplaywidth(" " .. p[1] .. " ") end
  end
  local out, last = { "%#Gml8" .. color .. "Badge#" .. esc(head) }, "Badge"
  for _, p in ipairs(parts) do
    if p[1] ~= "" then
      local text = p[3] and fit(p[1], math.max(1, api.nvim_win_get_width(w) - fixed - 2)) or p[1]
      out[#out + 1] = "%#Gml8" .. color .. p[2] .. "# " .. esc(text) .. " "
      last = p[2]
    end
  end
  if last ~= "Bar" then out[#out + 1] = "%#Gml8" .. color .. "Bar#" end
  return table.concat(out)
end

local function refresh_bars()
  if not S then return end
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then
      vim.wo[w].winbar = CHIPS[key] and chips(COLOR[key], BADGE[key], CHIPS[key](), w) or bar(COLOR[key], BADGE[key], net_status(), w)
    end
  end
  local w = S.wins.inv
  if valid_win(w) then
    local b = api.nvim_win_get_buf(w)
    local mod = valid_buf(b) and vim.bo[b].modified
    -- INVENTORY · the file · ⚡n (gml8's own services that run), then the bar to the edge
    vim.wo[w].winbar = chips("Inv", "INVENTORY", {
      { fn.fnamemodify(S.inv_path, ":t") .. (mod and " [+]" or ""), "Mid", true },
      { "⚡" .. tostring(tonumber((S.state or {}).launched) or 0), "Dark" },
    }, w)
  end
  local m = S.idx_msg or {}
  w = S.wins.idx
  if valid_win(w) then
    -- MODEL IDX · the entry · its run (⚡ :8090) · ✎ staged on the bar (the keys: the vimpage's help)
    vim.wo[w].winbar = chips("Idx", "MODEL IDX", {
      { nn(m.title) and tostring(m.title) or "", "Mid", true },
      { nn(m.run) and tostring(m.run) or "", "Dark" },
      { m.staged and "✎ staged" or "", "Bar" },
    }, w)
  end
  w = S.wins.stage
  if valid_win(w) then
    vim.wo[w].winbar = bar("Stage", "STAGE", (m.key and (tostring(m.key) .. " · ") or "")
      .. (m.staged and "✎ differs from the entry · C commits · L launches it · u drops it" or "= the entry · edit to stage a change")
      .. " · q closes", w)
  end
  w = S.wins.tail
  if valid_win(w) then
    local live = m.active
    vim.wo[w].winhighlight = live and "WinBar:Gml8TailBar,WinBarNC:Gml8TailBar" or "WinBar:Gml8TailOffBar,WinBarNC:Gml8TailOffBar"
    -- LOG · the log file · ● live / ○ ended (the whole header greys when the service has ended)
    vim.wo[w].winbar = chips(live and "Tail" or "TailOff", "LOG", {
      { fn.fnamemodify(tostring(S.tail_log or ""), ":t"), "Mid", true },
      { live and "● live" or "○ ended", "Dark" },
    }, w)
  end
end

-- ---------------------------------------------------------------- layout
local function pane_opts(w, wrap)
  vim.wo[w].wrap = wrap
  vim.wo[w].number, vim.wo[w].relativenumber, vim.wo[w].list = false, false, false
  vim.wo[w].signcolumn, vim.wo[w].foldcolumn = "no", "0"
  vim.wo[w].cursorline = false
end

local function frac(v, default, whole)
  local x = tonumber(v) or default
  return x < 1 and math.floor(whole * x) or math.floor(x)
end

local function sizes()
  if not (S and in_tab()) then return end
  local avail = vim.o.lines - vim.o.cmdheight - 1
  local h = math.max(6, math.min(frac(vim.g.gml8_bottom_height, 0.34, avail), avail - 10))
  local cols = vim.o.columns
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then pcall(api.nvim_win_set_height, w, h); vim.wo[w].winfixheight = true end
  end
  if valid_win(S.wins.log) then pcall(api.nvim_win_set_width, S.wins.log, frac(vim.g.gml8_llm_width, 0.34, cols)) end
  if valid_win(S.wins.iface) then pcall(api.nvim_win_set_width, S.wins.iface, frac(vim.g.gml8_iface_width, 0.26, cols)) end
  if valid_win(S.wins.inv) then pcall(api.nvim_win_set_width, S.wins.inv, frac(vim.g.gml8_inv_width, 0.5, cols)) end
  -- the right column: MODEL IDX takes what STAGE (its lines, ≤ 30%) and LOG (g:gml8_log_height, 28%) leave
  local top = avail - h - 1
  local n_stage = valid_buf(S.bufs.stage) and api.nvim_buf_line_count(S.bufs.stage) or 8
  local sh = math.max(4, math.min(n_stage, math.floor(top * 0.3)))
  -- bottom up, each pinned (winfixheight): what a pinned window gives or takes goes to MODEL IDX
  for _, k in ipairs({ "tail", "stage" }) do
    local w = S.wins[k]
    if valid_win(w) then
      local want = k == "stage" and sh or math.max(4, frac(vim.g.gml8_log_height, 0.28, top))
      if k == "tail" then sh = math.max(4, math.min(sh, top - want - 10)) end
      vim.wo[w].winfixheight = false
      pcall(api.nvim_win_set_height, w, want)
      vim.wo[w].winfixheight = true
    end
  end
end

local function layout()
  if not S then return end
  if not (S.tab and api.nvim_tabpage_is_valid(S.tab)) then
    vim.cmd("tabnew")
    S.tab = api.nvim_get_current_tabpage()
    local scratch = api.nvim_get_current_buf()
    local w = api.nvim_get_current_win()
    api.nvim_win_set_buf(w, inv_buf())
    if api.nvim_buf_get_name(scratch) == "" and scratch ~= S.inv_buf then pcall(api.nvim_buf_delete, scratch, { force = true }) end
    S.wins = { inv = w }
  end
  if not in_tab() then return end
  local W = S.wins
  for _, key in ipairs({ "inv", "idx", "stage", "tail", "chat", "log", "iface" }) do
    if W[key] and not win_in_tab(W[key]) then W[key] = nil end
  end
  if not W.inv then
    W.inv = api.nvim_open_win(inv_buf(), false, valid_win(W.idx) and { split = "left", win = W.idx } or { split = "above", win = -1 })
  end
  if not W.chat then
    W.chat = api.nvim_open_win(S.bufs.chat, false, { split = "below", win = -1 })
    pcall(api.nvim_win_set_cursor, W.chat, { api.nvim_buf_line_count(S.bufs.chat), 0 })
  end
  if not W.log then
    W.log = api.nvim_open_win(S.bufs.log, false, { split = "right", win = W.chat })
    pcall(api.nvim_win_set_cursor, W.log, { api.nvim_buf_line_count(S.bufs.log), 0 })
  end
  if joined() and not W.iface then
    W.iface = api.nvim_open_win(S.bufs.iface, false, { split = "right", win = W.log })
  elseif not joined() and W.iface then
    if api.nvim_get_current_win() == W.iface then api.nvim_set_current_win(W.chat) end
    pcall(api.nvim_win_close, W.iface, true)
    W.iface = nil
  end
  if not W.idx then W.idx = api.nvim_open_win(S.bufs.idx, false, { split = "right", win = W.inv }) end
  if S.stage_open and not W.stage then
    W.stage = api.nvim_open_win(S.bufs.stage, false, { split = "below", win = W.idx })
  elseif not S.stage_open and W.stage then
    if api.nvim_get_current_win() == W.stage then api.nvim_set_current_win(W.idx) end
    pcall(api.nvim_win_close, W.stage, true)
    W.stage = nil
  end
  local want_tail = S.tail_log and not S.tail_hidden and valid_buf(S.bufs.tail)
  if want_tail and not W.tail then
    W.tail = api.nvim_open_win(S.bufs.tail, false, { split = "below", win = valid_win(W.stage) and W.stage or W.idx })
  elseif want_tail and api.nvim_win_get_buf(W.tail) ~= S.bufs.tail then
    api.nvim_win_set_buf(W.tail, S.bufs.tail)
  elseif not want_tail and W.tail then
    if api.nvim_get_current_win() == W.tail then api.nvim_set_current_win(W.idx) end
    pcall(api.nvim_win_close, W.tail, true)
    W.tail = nil
  end
  for _, key in ipairs(BOTTOM) do
    local w = W[key]
    if valid_win(w) then
      pane_opts(w, true)
      vim.wo[w].winhighlight = ("WinBar:Gml8%s,WinBarNC:Gml8%s"):gsub("%%s", COLOR[key] .. "Bar")
    end
  end
  -- INVENTORY: the file itself, with line numbers (a reserved line's number is its series slot)
  local iw = W.inv
  vim.wo[iw].number, vim.wo[iw].relativenumber = true, false
  vim.wo[iw].cursorline, vim.wo[iw].wrap, vim.wo[iw].signcolumn = true, false, "yes:1"
  vim.wo[iw].winhighlight = "WinBar:Gml8InvBar,WinBarNC:Gml8InvBar"
  pane_opts(W.idx, false)
  vim.wo[W.idx].winhighlight = "WinBar:Gml8IdxBar,WinBarNC:Gml8IdxBar"
  if valid_win(W.stage) then
    pane_opts(W.stage, false)
    vim.wo[W.stage].winhighlight = "WinBar:Gml8StageBar,WinBarNC:Gml8StageBar"
  end
  if valid_win(W.tail) then pane_opts(W.tail, false) end
  sizes()
  refresh_bars()
end

local function refresh()
  if not S then return end
  if in_tab() then layout() else refresh_bars() end
end

-- ---------------------------------------------------------------- the inventory's marks
local function paint_inv()
  if not (S and S.marks) then return end
  local b = valid_buf(S.inv_buf) and S.inv_buf or nil
  if not b then return end
  api.nvim_buf_clear_namespace(b, NS_INV, 0, -1)
  local n = api.nvim_buf_line_count(b)
  for k, m in pairs(S.marks) do
    local row = tonumber(k) - 1
    if row >= 0 and row < n then
      local opts = { priority = 80 }
      if nn(m.parts) then
        -- chips at the window's right edge: the labels in the state's colours, the values inverted in their own
        local state = "Gml8Edge" .. ((nn(m.hl) and tostring(m.hl) or "Gml8Dim"):gsub("^Gml8", ""))
        local vt = {}
        for _, p in ipairs(m.parts) do
          local g = p[2] == "port" and (state .. "Inv") or p[2] == "404" and "Gml8Edge404" or state
          vt[#vt + 1] = { " " .. tostring(p[1]) .. " ", g }
        end
        opts.virt_text, opts.virt_text_pos = vt, "right_align"
      elseif nn(m.text) then
        -- a state at the window's right edge (inventory lines are long: an end-of-line mark would be off screen), in
        -- its Gml8Edge<state> colours; the hints of short lines (placeholders, empty slots) right after the text
        local hint = m.hl == "Gml8Placeholder"
        local hl = hint and m.hl or ("Gml8Edge" .. ((nn(m.hl) and tostring(m.hl) or "Gml8Dim"):gsub("^Gml8", "")))
        opts.virt_text = { { hint and ("  " .. m.text) or (" " .. m.text .. " "), hl } }
        opts.virt_text_pos = hint and "eol" or "right_align"
      end
      if nn(m.sign) then opts.sign_text, opts.sign_hl_group = m.sign, nn(m.sign_hl) and m.sign_hl or nil end
      if nn(m.line_hl) then opts.line_hl_group = m.line_hl end
      pcall(api.nvim_buf_set_extmark, b, NS_INV, row, 0, opts)
    end
  end
end

-- ---------------------------------------------------------------- MODEL IDX, STAGE, LOG
local function stop_tail()
  if S.tail_job then pcall(fn.jobstop, S.tail_job) end
  S.tail_job = nil
  local old = S.bufs.tail
  S.bufs.tail = nil
  if valid_win(S.wins.tail) and valid_buf(old) then
    -- keep the window until the new buffer is in it (no flicker); a placeholder when there is none
    local ph = api.nvim_create_buf(false, true)
    vim.bo[ph].bufhidden = "wipe"
    api.nvim_win_set_buf(S.wins.tail, ph)
  end
  if valid_buf(old) then pcall(api.nvim_buf_delete, old, { force = true }) end
end

local function open_tail(path)
  if S.tail_log == path and valid_buf(S.bufs.tail) then return end
  stop_tail()
  S.tail_log = path
  if not path then return end
  local b = api.nvim_create_buf(false, true)
  vim.bo[b].bufhidden = "hide"
  local cmd = { "tail", "-n", "300", "-F", path }
  local ok, job = pcall(api.nvim_buf_call, b, function()
    if fn.has("nvim-0.11") == 1 then return fn.jobstart(cmd, { term = true }) end
    return fn.termopen(cmd)
  end)
  if not ok or not job or job <= 0 then
    pcall(api.nvim_buf_delete, b, { force = true })
    S.tail_log = nil
    return
  end
  S.tail_job = job
  S.bufs.tail = b
  pcall(api.nvim_buf_set_name, b, "gml8://log/" .. fn.fnamemodify(path, ":t"))
  M._tail_keys(b)
end

local function set_idx(msg)
  local b = S.bufs.idx
  vim.bo[b].modifiable = true
  api.nvim_buf_set_lines(b, 0, -1, false, msg.lines or {})
  vim.bo[b].modifiable = false
  api.nvim_buf_clear_namespace(b, NS_IDX, 0, -1)
  for _, h in ipairs(msg.hls or {}) do
    pcall(api.nvim_buf_set_extmark, b, NS_IDX, h[1], h[2], { end_col = h[3], hl_group = h[4], priority = 70 })
  end
end

local function set_stage(lines, key)
  local b = S.bufs.stage
  S.stage_key = key
  api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].modified = false
  S.stage_tick = api.nvim_buf_get_changedtick(b)
end

local function on_idx(msg)
  local prev = S.idx_msg
  S.idx_msg = msg
  set_idx(msg)
  if type(msg.stage) == "table" then set_stage(msg.stage, msg.key) end
  local logp = nn(msg.log) and msg.log or nil
  if logp ~= S.tail_log then
    open_tail(logp)
    refresh()
  elseif not prev or prev.active ~= msg.active then
    refresh()
  else
    refresh_bars()
  end
end

local function stage_text()
  if not (S and valid_buf(S.bufs.stage)) then return nil end
  return table.concat(api.nvim_buf_get_lines(S.bufs.stage, 0, -1, false), "\n")
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

local ORDER = { "inv", "idx", "stage", "tail", "chat", "log", "iface" }
local function cycle(delta)
  if not (S and in_tab()) then return end
  local list = {}
  for _, k in ipairs(ORDER) do if valid_win(S.wins[k]) then list[#list + 1] = k end end
  if #list == 0 then return end
  local i = 1
  for n, k in ipairs(list) do if S.wins[k] == api.nvim_get_current_win() then i = n end end
  M.focus(list[(i - 1 + delta) % #list + 1])
end

function M.show()
  if not S then return M.start() end
  if not in_tab() then
    S.prev_tab = api.nvim_get_current_tabpage()
    S.anchor = api.nvim_get_current_win()
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) then api.nvim_set_current_tabpage(S.tab) end
  layout()
  M.focus("inv")
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

-- ---------------------------------------------------------------- selection (INVENTORY -> MODEL IDX)
local function select_line(force)
  if not (S and S.job) then return end
  local w = api.nvim_get_current_win()
  local b = api.nvim_win_get_buf(w)
  if not is_inv(b) then return end
  local row = api.nvim_win_get_cursor(w)[1]
  local text = api.nvim_buf_get_lines(b, row - 1, row, false)[1] or ""
  if not force and S.sel_line == row and S.sel_text == text then return end
  S.sel_line, S.sel_text = row, text
  send({ t = "op", op = "select", line = row, text = text, modified = vim.bo[b].modified })
end

local select_timer
local function select_soon()
  if select_timer then select_timer:stop() end
  select_timer = vim.defer_fn(function() select_timer = nil; select_line(false) end, 120)
end

local stage_timer
local function stage_changed()
  if not (S and valid_buf(S.bufs.stage)) then return end
  if api.nvim_buf_get_changedtick(S.bufs.stage) == S.stage_tick then return end
  if stage_timer then stage_timer:stop() end
  stage_timer = vim.defer_fn(function()
    stage_timer = nil
    if S and S.stage_key then send({ t = "op", op = "stage", key = S.stage_key, text = stage_text() }) end
  end, 250)
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
  end
  if S and S.idx_msg then ctx.selected = S.idx_msg.key end
  return ctx
end

local OPS = {}
function OPS.context() return context() end
function OPS.ex(p)
  local win = anchor_win() or api.nvim_get_current_win()
  return api.nvim_win_call(win, function() return api.nvim_exec2(p.command, { output = true }).output end)
end
function OPS.lua(p)
  local chunk, err = load(p.code, "gml8-agent", "t")
  if not chunk then error(err) end
  local ok, res = pcall(api.nvim_win_call, anchor_win() or api.nvim_get_current_win(), chunk)
  if not ok then error(res) end
  return res ~= nil and vim.inspect(res) or ""
end
--- The agent wrote the inventory (merge, commit, an adaptation): buffers showing it reload (unless unsaved).
function OPS.reload(p)
  local path = p.path and fn.fnamemodify(p.path, ":p")
  for _, b in ipairs(api.nvim_list_bufs()) do
    if api.nvim_buf_is_loaded(b) and path and fn.fnamemodify(api.nvim_buf_get_name(b), ":p") == path then
      if vim.bo[b].modified then
        notify("the inventory changed on disk; your buffer has unsaved changes (:e! to reload)", vim.log.levels.WARN)
      else
        pcall(api.nvim_buf_call, b, function() vim.cmd("silent! checktime") end)
      end
    end
  end
  vim.schedule(function() paint_inv(); if S and in_tab() then select_line(true) end end)
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
      append_raw(b, msg.text, nn(msg.hl) and msg.hl or nil)
    else
      if msg.eos then S.raw_row = nil end
      if not msg.eos or (msg.text or "") ~= "" then append(b, msg.text, nn(msg.hl) and msg.hl or nil) end
    end
  elseif msg.t == "state" then
    local was = joined()
    S.state = msg
    if joined() ~= was then refresh() else refresh_bars() end
  elseif msg.t == "inv" then
    S.marks = msg.marks or {}
    paint_inv()
  elseif msg.t == "idx" then
    on_idx(msg)
  elseif msg.t == "services" then
    if bus then bus.publish(msg.list or {}) end
  elseif msg.t == "provided" then
    if bus then bus._provided(msg) end
  elseif msg.t == "req" then
    handle_req(msg)
  elseif msg.t == "show" then
    M.show()
    M.focus("chat")
  elseif msg.t == "alert" then
    if not in_tab() then notify(msg.text or "", vim.log.levels.WARN) end
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
  for _, l in ipairs(data) do if l ~= "" then append("log", l, "ErrorMsg") end end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  if bus then bus.detach() end
  append("chat", "gml8 stopped (exit " .. tostring(code) .. "). The services keep running; :Gml8 restarts it.",
    code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh()
end

-- ---------------------------------------------------------------- input and keys
local function submit(text, ctx)
  text = vim.trim(text or "")
  if text == "" then return end
  if not (S and S.job) then return notify("not running (:Gml8)", vim.log.levels.WARN) end
  send({ t = "input", text = text, ctx = ctx or context() })
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

--- The inventory is the user's file: <CR> and <Tab>/<S-Tab> act only in gml8's INVENTORY window.
function M._inv_keys(b)
  local function in_inv() return S and in_tab() and api.nvim_get_current_win() == S.wins.inv end
  local o = { buffer = b, silent = true, expr = true }
  vim.keymap.set("n", "<CR>", function() if in_inv() then vim.schedule(function() M.focus("idx") end); return "" end return "<CR>" end, o)
  vim.keymap.set("n", "<Tab>", function() if in_inv() then vim.schedule(function() cycle(1) end); return "" end return "<Tab>" end, o)
  vim.keymap.set("n", "<S-Tab>", function() if in_inv() then vim.schedule(function() cycle(-1) end); return "" end return "<S-Tab>" end, o)
end

local function cur_op(op, extra)
  local m = S and S.idx_msg
  if not (m and m.key and m.launchable) then return notify("no entry selected (move the cursor in INVENTORY)", vim.log.levels.WARN) end
  send(vim.tbl_extend("force", { t = "op", op = op, key = m.key, line = m.line, stage = S.stage_key == m.key and stage_text() or nil,
    modified = valid_buf(S.inv_buf) and vim.bo[S.inv_buf].modified or false }, extra or {}))
end

function M._idx_keys(b)
  M._pane_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "L", function() cur_op("launch") end, o)
  vim.keymap.set("n", "R", function() cur_op("relaunch") end, o)
  vim.keymap.set("n", "x", function() cur_op("stop") end, o)
  vim.keymap.set("n", "C", function() cur_op("commit") end, o)
  vim.keymap.set("n", "u", function() cur_op("unstage") end, o)
  vim.keymap.set("n", "ss", function() M.open_stage() end, { buffer = b, silent = true })
  vim.keymap.set("n", "e", function()
    if S.idx_msg and S.idx_msg.line and valid_win(S.wins.inv) then
      api.nvim_set_current_win(S.wins.inv)
      pcall(api.nvim_win_set_cursor, S.wins.inv, { S.idx_msg.line, 0 })
    end
  end, o)
  vim.keymap.set("n", "l", function() M.toggle_log() end, o)
  vim.keymap.set("n", "r", function() send({ t = "op", op = "hw" }); send({ t = "op", op = "probe" }) end, o)
  vim.keymap.set("n", "m", function() send({ t = "op", op = "merge" }) end, o)
  vim.keymap.set("n", "y", function()
    for _, l in ipairs(api.nvim_buf_get_lines(b, 0, -1, false)) do
      local c = l:match("^%s*%$ (.*)$")
      if c then fn.setreg('"', c); fn.setreg("+", c); return notify("yanked: " .. c:sub(1, 80) .. "…") end
    end
  end, o)
  vim.keymap.set("n", "a", function()
    local key = S.idx_msg and S.idx_msg.key or ""
    M.focus("chat")
    vim.schedule(function() api.nvim_feedkeys(key .. " ", "n", false) end)
  end, o)
end

--- STAGE shows only when asked: ss in MODEL IDX opens it (and goes there), q in it closes it.
function M.open_stage()
  if not S then return end
  S.stage_open = true
  layout()
  M.focus("stage")
end

function M.close_stage()
  if not S then return end
  S.stage_open = false
  layout()
  M.focus("idx")
end

function M._stage_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "q", function() M.close_stage() end, o)
  vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
  vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
  vim.keymap.set("n", "<CR>", function() M.focus("idx") end, o)
  vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
end

function M._tail_keys(b)
  M._pane_keys(b)
  vim.keymap.set("n", "x", function() M.toggle_log() end, { buffer = b, silent = true, nowait = true })
end

function M.toggle_log()
  if not S then return end
  S.tail_hidden = not S.tail_hidden
  layout()
end

local function create_buffers()
  local chat = api.nvim_create_buf(false, true)
  vim.bo[chat].buftype = "prompt"
  vim.bo[chat].bufhidden = "hide"
  vim.bo[chat].filetype = "gml8chat"
  pcall(api.nvim_buf_set_name, chat, "gml8://chat")
  fn.prompt_setprompt(chat, PROMPT)
  fn.prompt_setcallback(chat, function(text)
    vim.schedule(function()
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
  for key, ft in pairs({ log = "gml8log", iface = "gml8iface", idx = "gml8idx" }) do
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].filetype = ft
    vim.bo[b].modifiable = false
    pcall(api.nvim_buf_set_name, b, "gml8://" .. (key == "log" and "llm" or key))
    bufs[key] = b
    if key == "idx" then M._idx_keys(b) else M._pane_keys(b) end
  end
  vim.bo[bufs.idx].modifiable = true
  api.nvim_buf_set_lines(bufs.idx, 0, -1, false, { " MODEL IDX — move the cursor to an entry in INVENTORY" })
  vim.bo[bufs.idx].modifiable = false
  local stage = api.nvim_create_buf(false, true)
  vim.bo[stage].bufhidden = "hide"
  vim.bo[stage].filetype = "yaml"
  pcall(api.nvim_buf_set_name, stage, "gml8://stage")
  api.nvim_buf_set_lines(stage, 0, -1, false, { "# STAGE — the selected entry as fields: edit them, MODEL IDX re-measures" })
  bufs.stage = stage
  M._stage_keys(stage)
  api.nvim_create_autocmd({ "TextChanged", "TextChangedI" }, { buffer = stage, callback = stage_changed })
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
  args = args or vim.g.gml8_args or {}
  if type(args) == "string" then args = vim.split(args, "%s+", { trimempty = true }) end
  local out = {}
  for _, a in ipairs(args) do out[#out + 1] = tostring(a) end
  return out
end

function M.start(args, opts)
  opts = opts or {}
  if S and S.job then
    if opts.show ~= false then M.toggle() end
    return
  end
  args = normalize_args(args)
  local py = vim.g.gml8_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local inv = inventory_path()
  local cmd = { py, "-u", SCRIPT_DIR .. "/gml8.py", "--inventory", inv }
  if vim.g.gml8_memory then vim.list_extend(cmd, { "--memory", native(fn.fnamemodify(fn.expand(vim.g.gml8_memory), ":p")) }) end
  vim.list_extend(cmd, args)
  local old = S
  S = { bufs = old and old.bufs or create_buffers(), wins = old and old.wins or {}, tab = old and old.tab, state = {},
    inv_path = inv, inv_buf = old and old.inv_buf, marks = {},
    prev_tab = old and old.prev_tab or api.nvim_get_current_tabpage(), anchor = old and old.anchor or api.nvim_get_current_win() }
  if not in_tab() then S.prev_tab, S.anchor = api.nvim_get_current_tabpage(), api.nvim_get_current_win() end
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = LOAD_CWD, env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    return notify("cannot start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
  end
  if bus then
    bus.attach({ request = function(want, rid) send({ t = "op", op = "provide", rid = rid, want = want }) end })
  end
  if opts.show ~= false then M.show() end
end

local function ensure_started()
  if not (S and S.job) then M.start(nil, { show = false }) end
  return S and S.job
end

function M.ask(text)
  if not ensure_started() then return end
  submit(text)
end

--- :Gml8Launch / :Gml8Relaunch / :Gml8Stop / :Gml8Commit [ref]: the ref given; else the line under the cursor when it
--- is the inventory's; else the entry in MODEL IDX; else ask.
function M.entry_op(op, ref)
  if not ensure_started() then return end
  ref = vim.trim(ref or "")
  if ref ~= "" then
    if op == "stop" and ref:match("^%d%d%d%d+$") then return send({ t = "op", op = "stop", port = tonumber(ref) }) end
    return send({ t = "op", op = op, ref = ref })
  end
  local b = api.nvim_get_current_buf()
  if is_inv(b) then
    local row = api.nvim_win_get_cursor(0)[1]
    local text = api.nvim_buf_get_lines(b, row - 1, row, false)[1] or ""
    local m = S.idx_msg
    local stage = (m and m.line == row and S.stage_key == m.key) and stage_text() or nil
    send({ t = "op", op = op, line = row, text = text, stage = stage, modified = vim.bo[b].modified })
    if not in_tab() then notify(op .. " line " .. row .. " (gml8k shows the gml8 tab)") end
    return
  end
  if S.idx_msg and S.idx_msg.launchable then return cur_op(op) end
  vim.ui.input({ prompt = "gml8: " .. op .. " which entry (11, @name)? " }, function(v)
    if v and vim.trim(v) ~= "" then send({ t = "op", op = op, ref = vim.trim(v) }) end
  end)
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  if S.tail_job then pcall(fn.jobstop, S.tail_job) end
  if bus then bus.detach() end
  if valid_buf(S.inv_buf) then pcall(api.nvim_buf_clear_namespace, S.inv_buf, NS_INV, 0, -1) end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) and #api.nvim_list_tabpages() > 1 then
    M.back()
    pcall(function() vim.cmd("tabclose " .. api.nvim_tabpage_get_number(S.tab)) end)
  end
  for _, b in pairs(S.bufs) do
    if valid_buf(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  S = nil
end

function M.status() return S and S.job and S.state.net or nil end
function M.running() return S ~= nil and S.job ~= nil end
M._session = function() return S end

local cmd = api.nvim_create_user_command
cmd("Gml8", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start gml8 / go to its tab / back" })
cmd("Gml8Toggle", function() M.toggle() end, { desc = "Go to the gml8 tab / back" })
cmd("Gml8Ask", function(o) M.ask(o.args) end, { nargs = "+", desc = "Ask gml8" })
for _, name in ipairs({ "Launch", "Relaunch", "Stop", "Commit" }) do
  cmd("Gml8" .. name, function(o) M.entry_op(name:lower(), o.args) end,
    { nargs = "?", desc = name .. " an entry (the inventory line under the cursor, the MODEL IDX entry, or ref)" })
end
cmd("Gml8Merge", function() if ensure_started() then send({ t = "op", op = "merge" }) end end, { desc = "Merge the series into the inventory" })
cmd("Gml8Log", function() M.toggle_log() end, { desc = "The LOG pane on/off" })
cmd("Gml8Cancel", function() send({ t = "cancel" }) end, {})
cmd("Gml8Quit", M.quit, { desc = "Stop gml8 (the LLM services keep running)" })

if vim.g.gml8_toggle_key then
  vim.keymap.set({ "n", "i", "t" }, vim.g.gml8_toggle_key, function()
    if fn.mode() == "t" or fn.mode() == "i" then vim.cmd("stopinsert") end
    M.toggle()
  end, { silent = true, desc = "gml8 tab: go / back" })
end

local aug = api.nvim_create_augroup("gml8", { clear = true })
api.nvim_create_autocmd("VimResized", { group = aug, callback = function() if in_tab() then sizes() end end })
api.nvim_create_autocmd("TabEnter", { group = aug, callback = function() if in_tab() then layout() end end })
api.nvim_create_autocmd("TabClosed", { group = aug, callback = function()
  if S and S.tab and not api.nvim_tabpage_is_valid(S.tab) then S.tab, S.wins = nil, {} end
end })
api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function() if S and S.job then send({ t = "quit" }) end end })
api.nvim_create_autocmd("WinEnter", { group = aug, callback = function()
  if S and in_tab() and api.nvim_get_current_win() ~= S.wins.chat and fn.mode() == "i"
    and api.nvim_get_current_win() ~= S.wins.stage then vim.cmd("stopinsert") end
end })
api.nvim_create_autocmd("WinClosed", { group = aug, callback = function(ev)
  if not S then return end
  local w = tonumber(ev.match)
  for key, x in pairs(S.wins) do
    if x == w then
      S.wins[key] = nil
      if key == "stage" then S.stage_open = false end -- closed by hand (:q): stays closed until ss
    end
  end
end })
api.nvim_create_autocmd({ "CursorMoved", "TextChanged", "TextChangedI", "BufEnter" }, { group = aug, callback = function(ev)
  if S and S.job and is_inv(ev.buf) then select_soon() end
end })
api.nvim_create_autocmd("BufWritePost", { group = aug, callback = function(ev)
  if S and S.job and is_inv(ev.buf) then
    send({ t = "op", op = "probe" })
    vim.defer_fn(function() paint_inv(); select_line(true); refresh_bars() end, 300)
  end
end })
api.nvim_create_autocmd("BufModifiedSet", { group = aug, callback = function(ev) if S and is_inv(ev.buf) then refresh_bars() end end })

package.loaded.gml8 = M

-- ---------------------------------------------------------------- Intelligence Network
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("gml8", {
      join = function(p)
        if S and S.job then
          send({ t = "net", op = "join", name = p.name, purpose = p.purpose ~= "" and p.purpose or nil, hub = p.hub, network = p.network.name })
        else
          M.start(vim.list_extend(normalize_args(nil), inet.agent_args(p)))
        end
      end,
      leave = function() if S and S.job then send({ t = "net", op = "leave" }) end end,
      status = function() return M.status() end,
    })
  end
end

-- `:source gml8.lua` (gml81) starts gml8 and opens its tab, unless g:gml8_no_autostart is set.
if MODNAME == nil and not vim.g.gml8_no_autostart then M.start() end

return M
