-- disp.lua: Neovim frontend of disp, the image agent (requires Neovim >= 0.10).
-- Loaded only through ~/echo-home/.vim/mimd/vimpage_disp.
--
-- The agent (disp.py, same folder) shows images to the user on the pad DISPAD, a Rust window (pad/: zoom, crop,
-- numbered marks, touching pixels, clipboard, the strip of every image ever loaded), looks at them through the local
-- multimodal LLM (llama.cpp serving a vision model: --mmproj) and MAPS them: a flat inventory of what
-- the eye recognises, turned into a DOM-like tree with text blocks and relations, kept per content in
-- ~/echo-home/memory/gddr9b with the notes and the trace of what you did on the pad (reloaded when it comes back).
--
-- disp has its own tab page:
--     ┌─ 3fa2  #3  sign.png 480×320 · 24 elements ─┬─ GRAPH  root 3fa2#3 · context "…" ───────┐
--     │ MAP: the image in focus — identity, summary, │ the memory graph around it: its concepts, │   top: the image in
--     │ TREE, TEXT, RELATIONS, INVENTORY, NOTES,     │ its knowledge, the memories associated    │   focus and its
--     │ TRACE                                        │ (and how they link)                       │   memory graph
--     ├─ DISP  🖼 3fa2#3 · ○ idle ────────────────┬──┴─ LLM  raw output · 👁 eye ──────┬─ IFACE ─┤
--     │ what disp does, your input on the last    │ the raw LLM output and what the     │ network │   bottom: DISP · LLM ·
--     │ line                                      │ eye sees                            │         │   IFACE (while joined)
--     └───────────────────────────────────────────┴─────────────────────────────────────┴─────────┘
-- MAP and GRAPH follow the pad's focus (a click on its strip, an image disp loads). GRAPH is the memory graph queried
-- with the image in focus as root and the current request as context; it is in the request's context too ([GRAPH]).
-- Each header is a colored badge and strip across the pane.
--
-- `:source disp.lua` (disp1) starts disp and opens its tab. Sourced again while disp runs, it only goes to the tab;
-- once disp is stopped (:DispQuit), sourcing reloads this file fresh.
--
--   :Disp [args]       start (backend args from g:disp_args) / go to the disp tab / back to your tab
--   :DispToggle        the same (vimpage_disp maps it to dispk)
--   :DispOpen [path]   show an image on the pad: the path given, else the image under the cursor (a path in the
--                      text, a netrw entry, the buffer's own file), else it asks
--   :DispPaste         the image on the clipboard to the pad          :DispFocus <n>   bring image #n to the front
--   :DispMap [n]       bring image #n into focus (MAP, GRAPH follow); no n: go to MAP   :DispMapBuild  map it again
--   :DispPad           show / start the pad      :DispPadQuit  close it
--   :DispAsk <text>    send a request without going to the tab
--   :DispBuild         build the pad (cargo build --release in a terminal tab)
--   :DispCancel        cancel the running task (and a map being built)       :DispQuit  stop the backend (the pad stays)
--   :InetJoin disp / :InetLeave disp    (profile g:inet_agents.disp in vimpage_inet, optional)
--
-- Options: g:disp_python ("python"), g:disp_args (list), g:disp_memory (the memory disp and the pad share,
--          ~/echo-home/memory/gddr9b; images stay where they are), g:disp_pad_port (7791), g:disp_pad_bin,
--          g:disp_no_autostart, g:disp_map_width (0.55: MAP's share of the top row, GRAPH the rest),
--          g:disp_bottom_height (0.42: rows, or a fraction of the screen), g:disp_llm_width (0.36) and
--          g:disp_iface_width (0.28): fractions of the width,
--          g:disp_toggle_key (e.g. '<M-d>').
-- In the tab: last line of DISP is the input · <CR> send · <C-c> cancel task · <Tab> (normal) next pane ·
--             <S-Tab> previous pane · i input · q back to your tab (normal)
-- In MAP: <CR> on an element (e5 …) shows it on the pad · m marks it there · M maps the image again ·
--             gr the pad's grid layer on/off · r redraws
-- In GRAPH: <CR> on an image key (3fa2#3) brings that image into focus · r recomputes the graph view

local MODNAME = ...

-- Sourced again: a running disp keeps its session (only its tab is shown); a stopped one gives way to this load.
do
  local old = package.loaded.disp
  if MODNAME == nil and type(old) == "table" then
    local alive = old.running
    if type(alive) == "function" and alive() then
      old.show()
      return old
    end
    if type(old.quit) == "function" then pcall(old.quit) end -- close its tab, drop its buffers
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
local PAD_BIN = SCRIPT_DIR .. "/pad/target/release/disppad" .. (fn.has("win32") == 1 and ".exe" or "")
local NS = api.nvim_create_namespace("disp")
local NS_MAP = api.nvim_create_namespace("disp_map")
local NS_HIT = api.nvim_create_namespace("disp_map_hit")
-- The input prompt " DISPlay ": " DIS" as the DISP header badge, "Play " on the header's strip (DispPrompt / DispPromptInv).
local PROMPT_PARTS = { { " DIS", "DispPrompt" }, { "Play ", "DispPromptInv" } }
local PROMPT = PROMPT_PARTS[1][1] .. PROMPT_PARTS[2][1]
local BOTTOM = { "chat", "log", "iface" }                   -- the bottom panes, left to right (buffer keys)
local BADGE = { chat = "DISP", log = "LLM", iface = "IFACE" }
local COLOR = { chat = "Main", log = "Llm", iface = "Iface" } -- Disp<COLOR>Bar / Disp<COLOR>Badge
local IMAGE_EXT = { png = true, jpg = true, jpeg = true, gif = true, webp = true, bmp = true, tif = true, tiff = true,
  ico = true }
local S -- session; nil when not running

-- The prompt label: " DIS" as the DISP header badge, "Play " on its strip. The pane headers: a badge and a strip per
-- pane: DISP in burnt orange (gruvbox's orange: pixels, light), LLM in gruvbox's blue (as in hbmx/knbg), IFACE in its
-- purple; MAP panes in gruvbox's aqua, the image in focus on the pad with the DISP orange badge.
-- Override with  :hi DispPrompt ... / DispMainBar / DispMainBadge / DispLlmBar / DispMapBadge / DispMapFocusBadge ...
-- (defined as defaults, re-applied after a colorscheme change).
local function set_hl()
  local d = function(name, spec) spec.default = true; api.nvim_set_hl(0, name, spec) end
  local light = vim.o.background == "light"
  -- bar: the strip (fg: its text, else the colorscheme's), badge / on: the badge and its text
  local P = light and {
    Main = { bar = "#f9dcc0", fg = "#5a2101", badge = "#af3a03", on = "#fbf1c7", cbar = 223, cfg = 52, cb = 130, con = 230 },
    Llm = { bar = "#cadbea", badge = "#076678", on = "#fbf1c7", cbar = 153, cb = 24, con = 230 },
    Iface = { bar = "#e7cfdc", badge = "#8f3f71", on = "#fbf1c7", cbar = 225, cb = 96, con = 230 },
    Map = { bar = "#d5e6d3", fg = "#1d3b2a", badge = "#427b58", on = "#fbf1c7", cbar = 194, cfg = 22, cb = 29, con = 230 },
    MapFocus = { bar = "#d5e6d3", fg = "#1d3b2a", badge = "#af3a03", on = "#fbf1c7", cbar = 194, cfg = 22, cb = 130, con = 230 },
    Graph = { bar = "#f2e5bc", fg = "#5a4209", badge = "#b57614", on = "#fbf1c7", cbar = 230, cfg = 94, cb = 136, con = 230 },
  } or {
    Main = { bar = "#4a2a14", fg = "#fbdcb8", badge = "#fe8019", on = "#1d2021", cbar = 52, cfg = 223, cb = 208, con = 234 },
    Llm = { bar = "#2a3b48", badge = "#83a598", on = "#1d2021", cbar = 17, cb = 109, con = 234 },
    Iface = { bar = "#43304a", badge = "#d3869b", on = "#1d2021", cbar = 53, cb = 175, con = 234 },
    Map = { bar = "#263b30", fg = "#cfe8d0", badge = "#8ec07c", on = "#1d2021", cbar = 22, cfg = 194, cb = 108, con = 234 },
    MapFocus = { bar = "#263b30", fg = "#cfe8d0", badge = "#fe8019", on = "#1d2021", cbar = 22, cfg = 194, cb = 208, con = 234 },
    Graph = { bar = "#3c3418", fg = "#f2e5bc", badge = "#fabd2f", on = "#1d2021", cbar = 58, cfg = 230, cb = 214, con = 234 },
  }
  -- the input prompt reads like the DISP header: " DIS" as its badge, "Play " on its strip in the badge's colour
  local b = P.Main
  d("DispPrompt", { bg = b.badge, fg = b.on, bold = true, ctermbg = b.cb, ctermfg = b.con, cterm = { bold = true } })
  d("DispPromptInv", { bg = b.bar, fg = b.badge, bold = true, ctermbg = b.cbar, ctermfg = b.cb, cterm = { bold = true } })
  for name, p in pairs(P) do
    d("Disp" .. name .. "Bar", { bg = p.bar, fg = p.fg, ctermbg = p.cbar, ctermfg = p.cfg })
    d("Disp" .. name .. "Badge", { bg = p.badge, fg = p.on, bold = true, ctermbg = p.cb, ctermfg = p.con, cterm = { bold = true } })
  end
  -- the image number in a MAP header (" 3fa2 " then " #3 "): on a dark plate, in the badge's colour
  d("DispMapNum", { bg = "#1d2021", fg = light and "#689d6a" or "#8ec07c", bold = true, ctermbg = 234, ctermfg = 108,
    cterm = { bold = true } })
  d("DispMapFocusNum", { bg = "#1d2021", fg = "#fe8019", bold = true, ctermbg = 234, ctermfg = 208, cterm = { bold = true } })
  d("DispMapHead", { link = "Title" })
  d("DispMapSection", { link = "Statement" })
  d("DispMapId", { link = "Identifier" })
  d("DispMapLabel", { link = "String" })
  d("DispMapTree", { link = "NonText" })
  d("DispMapDim", { link = "Comment" })
  d("DispMapHit", { link = "CurSearch" })
end
set_hl()
api.nvim_create_autocmd("ColorScheme", { callback = function()
  set_hl()
  M._swatches = {}
end })
-- Painted at draw time on the chat buffer's input line, so it follows the prompt as the transcript grows.
local NS_PROMPT = api.nvim_create_namespace("disp_prompt")
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

local function notify(msg, level) vim.notify("disp: " .. msg, level or vim.log.levels.INFO) end

local LOAD_CWD = fn.getcwd()

local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function send(msg)
  if S and S.job then fn.chansend(S.job, vim.json.encode(msg) .. "\n") end
end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end
local function in_tab() return S ~= nil and S.tab ~= nil and api.nvim_tabpage_is_valid(S.tab)
  and api.nvim_get_current_tabpage() == S.tab end
local function win_in_tab(w) return valid_win(w) and S.tab ~= nil and api.nvim_win_get_tabpage(w) == S.tab end

-- ---------------------------------------------------------------- images in the editor
local function is_image(p)
  if not p or p == "" then return false end
  local ext = p:match("%.([%w]+)$")
  return ext ~= nil and IMAGE_EXT[ext:lower()] == true and fn.filereadable(p) == 1
end

--- The image the cursor is on in a window: a path in the text (relative to the buffer's folder or the cwd, ~ too),
--- a netrw entry, else the buffer's own file when it is an image. nil when none.
local function image_under_cursor(win)
  win = valid_win(win) and win or api.nvim_get_current_win()
  return api.nvim_win_call(win, function()
    local cands = {}
    local cfile = fn.expand("<cfile>")
    if cfile ~= "" then
      cfile = cfile:gsub("^file://", "")
      if vim.bo.filetype == "netrw" and vim.b.netrw_curdir then
        cands[#cands + 1] = vim.b.netrw_curdir .. "/" .. cfile
      end
      if cfile:sub(1, 1) == "~" or cfile:sub(1, 1) == "/" or cfile:match("^%a:[/\\]") then
        cands[#cands + 1] = fn.expand(cfile)
      else
        cands[#cands + 1] = fn.expand("%:p:h") .. "/" .. cfile
        cands[#cands + 1] = fn.getcwd() .. "/" .. cfile
      end
    end
    cands[#cands + 1] = fn.expand("%:p")
    for _, c in ipairs(cands) do
      if is_image(c) then return native(fn.fnamemodify(c, ":p")) end
    end
  end)
end

-- ---------------------------------------------------------------- buffers
--- Keep the windows that show this buffer (other than the one the user is in) at its end.
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
--- Lines appended meanwhile (a glance, the map's eye, the network's attention call) go below and are not split.
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

-- ---------------------------------------------------------------- headers
local function net_state()
  local net = S and S.state and S.state.net
  return type(net) == "table" and net or {}
end

local function joined() return net_state().joined == true end

--- DISP: the pad (image in focus), the map being built and the agent's status.
local function main_status()
  local st = S and S.state or {}
  local pad = type(st.pad) == "table" and st.pad or {}
  local parts = {}
  if pad.connected then
    local f = type(pad.focus) == "table" and pad.focus or nil
    parts[1] = f and ("🖼 " .. tostring(f.sha or "????"):sub(1, 4) .. "#" .. tostring(f.n) .. " " .. tostring(f.name)
      .. " of " .. tostring(pad.count)) or ("🖼 pad · " .. tostring(pad.count or 0) .. " images")
  else
    parts[1] = "🖼 pad closed"
  end
  parts[#parts + 1] = st.busy and ("● " .. tostring(st.phase or "busy")) or (S and S.job and "○ idle" or "✖ stopped")
  if st.mapping and st.mapping ~= vim.NIL then parts[#parts + 1] = "🗺 mapping " .. tostring(st.mapping) end
  if (st.queue or 0) > 0 then parts[#parts + 1] = st.queue .. " queued" end
  if st.auto then parts[#parts + 1] = "AUTO" end
  if st.map_auto == false then parts[#parts + 1] = "no auto-map" end
  if st.graph == false then parts[#parts + 1] = "🧠 off" end
  if not joined() then parts[#parts + 1] = "🌐 off" end
  if st.approval and st.approval ~= vim.NIL then parts[#parts + 1] = "❓ y/n/a" end
  return table.concat(parts, " · ")
end

--- IFACE: who disp is on which network.
local function net_status()
  local net = net_state()
  return "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
    .. " [" .. tostring(net.state) .. "]" .. ((net.members and net.connected) and (" · " .. net.members .. " members") or "")
end

--- LLM: the model the stream comes from.
local function llm_status()
  local model = (S and S.state or {}).model
  return "raw output · 👁 eye" .. ((model and model ~= vim.NIL) and (" · " .. fn.fnamemodify(tostring(model), ":t")) or "")
end

local STATUS = { chat = main_status, log = llm_status, iface = net_status }

--- Cut text to a display width, marking the cut with … (Neovim would cut a too-long bar on the left: the badge).
local function fit(s, width)
  if fn.strdisplaywidth(s) <= width then return s end
  local n = fn.strchars(s)
  while n > 0 and fn.strdisplaywidth(fn.strcharpart(s, 0, n)) > width - 1 do n = n - 1 end
  return fn.strcharpart(s, 0, n) .. "…"
end

local function bar(color, badge, text, w)
  local room = api.nvim_win_get_width(w) - fn.strdisplaywidth(badge) - 3
  return "%#Disp" .. color .. "Badge# " .. esc(badge) .. " %#Disp" .. color .. "Bar# " .. esc(fit(text, math.max(1, room)))
end

--- A MAP header: " 3fa2 " in the badge, " #3 " on a dark plate, then the title on the strip.
local function map_bar(color, key, text, w)
  local sha, num = key:match("^(%x+)(#%d+)$")
  if not sha then return bar(color, key, text, w) end
  local room = api.nvim_win_get_width(w) - fn.strdisplaywidth(sha) - fn.strdisplaywidth(num) - 5
  return "%#Disp" .. color .. "Badge# " .. sha .. " %#Disp" .. color .. "Num# " .. esc(num) .. " %#Disp" .. color
    .. "Bar# " .. esc(fit(text, math.max(1, room)))
end

local function refresh_bars()
  if not S then return end
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then vim.wo[w].winbar = bar(COLOR[key], BADGE[key], STATUS[key](), w) end
  end
  local w = S.wins.map
  if valid_win(w) then
    local m = S.focus_key and S.maps[S.focus_key]
    vim.wo[w].winhighlight = "WinBar:DispMapFocusBar,WinBarNC:DispMapFocusBar"
    vim.wo[w].winbar = m and map_bar("MapFocus", S.focus_key, "▶ " .. tostring(m.title or ""), w)
      or bar("Map", "MAP", "the image in focus on the pad (dispo / ===p / a click on the strip)", w)
  end
  w = S.wins.graph
  if valid_win(w) then
    vim.wo[w].winhighlight = "WinBar:DispGraphBar,WinBarNC:DispGraphBar"
    vim.wo[w].winbar = bar("Graph", "GRAPH", S.graph_title or "the memory graph around the image in focus", w)
  end
end

-- ---------------------------------------------------------------- the top row: MAP (image in focus) · GRAPH
--- A colour swatch ■#rrggbb is painted in its own colour.
local function swatch_hl(hex)
  M._swatches = M._swatches or {}
  local name = "DispC_" .. hex:sub(2)
  if not M._swatches[name] then
    api.nvim_set_hl(0, name, { fg = hex })
    M._swatches[name] = true
  end
  return name
end

local function paint_map(buf)
  api.nvim_buf_clear_namespace(buf, NS_MAP, 0, -1)
  local lines = api.nvim_buf_get_lines(buf, 0, -1, false)
  local function mark(row, s, e, group)
    pcall(api.nvim_buf_set_extmark, buf, NS_MAP, row, s - 1, { end_col = e, hl_group = group, priority = 90 })
  end
  for i, l in ipairs(lines) do
    local row = i - 1
    if i == 1 then
      mark(row, 1, #l, "DispMapHead")
    elseif l:match("^%u[%u ]+%f[%A]") and not l:match("^%u[%u ]+:") then
      mark(row, 1, #l, "DispMapSection")
    elseif l:match("^%l+: ") then
      local a = l:find(": ")
      mark(row, 1, a, "DispMapDim")
    end
    local a, b = l:find("^[│├└─ ]+")
    if a then mark(row, a, b, "DispMapTree") end
    local from = 1
    while true do -- element / block ids, and image keys 3fa2#3
      local s, e = l:find("%f[%w][eb]%d+[%.w%d]*", from)
      local s2, e2 = l:find("%f[%x]%x%x%x%x#%d+", from)
      if s2 and (not s or s2 < s) then s, e = s2, e2 end
      if not s then break end
      mark(row, s, e, "DispMapId")
      from = e + 1
    end
    from = 1
    while true do
      local s, e = l:find('"[^"]*"', from)
      if not s then break end
      mark(row, s, e, "DispMapLabel")
      from = e + 1
    end
    from = 1
    while true do -- graph: [concept] and —kind weight— links
      local s, e = l:find("%[[%w ][^%]]*%]", from)
      if not s then break end
      if not l:sub(s, e):match("^%[%d") then mark(row, s, e, "DispMapSection") end
      from = e + 1
    end
    from = 1
    while true do
      local s, e = l:find("—[%a/%-]+ [%d%.]+—", from)
      if not s then break end
      mark(row, s, e, "DispMapDim")
      from = e + 1
    end
    from = 1
    while true do
      local s, e, hex = l:find("■(#%x%x%x%x%x%x)", from)
      if not s then break end
      mark(row, s, s + 2, swatch_hl(hex)) -- ■ is 3 bytes
      mark(row, s + 3, e, "DispMapDim")
      from = e + 1
    end
  end
end

local function map_buf(key)
  local m = S.maps[key]
  if m and api.nvim_buf_is_valid(m.buf) then return m end
  local b = api.nvim_create_buf(false, true)
  vim.bo[b].bufhidden = "hide"
  vim.bo[b].filetype = "dispmap"
  vim.bo[b].modifiable = false
  pcall(api.nvim_buf_set_name, b, "disp://map/" .. key)
  m = { buf = b }
  S.maps[key] = m
  M._map_keys(b, key)
  return m
end

--- The element (e5, e5.w2) or text block (b2) id on the cursor line, the word under the cursor first.
local function element_at_cursor()
  local cword = fn.expand("<cWORD>")
  local word = cword:match("^%W*(e%d+[%.w%d]*)") or cword:match("^%W*(b%d+)%f[%W]")
  if word then return (word:gsub("[^%w%.]+$", "")) end
  local line = api.nvim_get_current_line()
  return line:match("%f[%w](e%d+[%.w%d]*)") or line:match("%f[%w](b%d+)%f[%W]")
end

local function placeholder()
  if not (S.bufs.mapnone and api.nvim_buf_is_valid(S.bufs.mapnone)) then
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    api.nvim_buf_set_lines(b, 0, -1, false, { "MAP", "",
      "The map of the image in focus on the pad (DISPAD): what the eye recognised in it (text lines and words, buttons,",
      "boxes, icons, shapes, figures, colour regions, patterns) as a tree, with its text blocks, relations, notes and",
      "the trace of what you did on the pad. It follows the focus: a click on the pad's strip, an image disp loads.",
      "", "Open an image: dispo on a path · ===p pastes the clipboard · Open… / ⌘V / drop on the pad." })
    vim.bo[b].modifiable = false
    S.bufs.mapnone = b
    M._map_keys(b, nil)
  end
  return S.bufs.mapnone
end

local GRAPH_NONE = { "GRAPH", "",
  "The memory graph around the image in focus: its concepts, its own knowledge, and the memories the graph",
  "associates with it (activation spreading from it and from what the current request is about), each with the path",
  "that links it. It follows the focus, and it is part of each request's context ([GRAPH]).",
  "When the associations change, they are kept with the image as knowledge (notes.yaml associations).",
  "", "<CR> on an image key (3fa2#3) brings that image into focus · r recomputes · q back to your tab" }

local function sizes()
  if not (S and in_tab()) then return end
  local avail = vim.o.lines - vim.o.cmdheight - 1
  local h = tonumber(vim.g.disp_bottom_height) or 0.42
  if h < 1 then h = math.floor(avail * h) end
  h = math.max(6, math.min(math.floor(h), avail - 6))
  local cols = vim.o.columns
  for _, key in ipairs(BOTTOM) do
    local w = S.wins[key]
    if valid_win(w) then
      pcall(api.nvim_win_set_height, w, h)
      vim.wo[w].winfixheight = true
    end
  end
  if valid_win(S.wins.log) then
    pcall(api.nvim_win_set_width, S.wins.log, math.floor(cols * (tonumber(vim.g.disp_llm_width) or 0.36)))
  end
  if valid_win(S.wins.iface) then
    pcall(api.nvim_win_set_width, S.wins.iface, math.floor(cols * (tonumber(vim.g.disp_iface_width) or 0.28)))
  end
  if valid_win(S.wins.map) and valid_win(S.wins.graph) then
    pcall(api.nvim_win_set_width, S.wins.map, math.floor(cols * (tonumber(vim.g.disp_map_width) or 0.55)))
  end
end

local function pane_opts(w, wrap)
  vim.wo[w].wrap = wrap
  vim.wo[w].number, vim.wo[w].relativenumber, vim.wo[w].list = false, false, false
  vim.wo[w].signcolumn, vim.wo[w].foldcolumn = "no", "0"
  vim.wo[w].cursorline = not wrap
end

--- The MAP pane shows the image in focus (its buffer; the placeholder when there is none).
local function show_focus_map()
  local w = S.wins.map
  if not valid_win(w) then return end
  local m = S.focus_key and S.maps[S.focus_key]
  local buf = m and m.buf or placeholder()
  if api.nvim_win_get_buf(w) ~= buf then api.nvim_win_set_buf(w, buf) end
  pane_opts(w, false)
end

--- Build what is missing of the tab: MAP (image in focus) | GRAPH on top, DISP · LLM · IFACE (IFACE while joined) below.
local function layout()
  if not S then return end
  if not (S.tab and api.nvim_tabpage_is_valid(S.tab)) then
    vim.cmd("tabnew")
    S.tab = api.nvim_get_current_tabpage()
    local scratch = api.nvim_get_current_buf()
    local w = api.nvim_get_current_win()
    api.nvim_win_set_buf(w, placeholder())
    if api.nvim_buf_get_name(scratch) == "" and scratch ~= S.bufs.mapnone then pcall(api.nvim_buf_delete, scratch, { force = true }) end
    S.wins = { map = w }
  end
  if not in_tab() then return end
  local W = S.wins
  for _, key in ipairs({ "map", "graph", "chat", "log", "iface" }) do
    if W[key] and not win_in_tab(W[key]) then W[key] = nil end
  end
  if not W.chat then
    W.chat = api.nvim_open_win(S.bufs.chat, false, { split = "below", win = -1 })
    pcall(api.nvim_win_set_cursor, W.chat, { api.nvim_buf_line_count(S.bufs.chat), 0 })
  end
  if not W.map then
    W.map = api.nvim_open_win(placeholder(), false, { split = "above", win = -1 })
  end
  if not W.graph then
    W.graph = api.nvim_open_win(S.bufs.graph, false, { split = "right", win = W.map })
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
  for _, key in ipairs(BOTTOM) do
    local w = W[key]
    if valid_win(w) then
      pane_opts(w, true)
      vim.wo[w].winhighlight = ("WinBar:Disp%s,WinBarNC:Disp%s"):gsub("%%s", COLOR[key] .. "Bar")
    end
  end
  show_focus_map()
  pane_opts(W.graph, false)
  sizes()
  refresh_bars()
end

local function refresh()
  if not S then return end
  if in_tab() then layout() else refresh_bars() end
end

--- The backend's MAP of one image: into its buffer (cursor kept); the MAP pane shows it when it is the pad's focus.
local function on_map(msg)
  local key = msg.key
  local m = map_buf(key)
  m.n, m.sha, m.title = msg.n, msg.sha, msg.title
  local keep = {}
  for _, w in ipairs(api.nvim_list_wins()) do
    if api.nvim_win_get_buf(w) == m.buf then keep[w] = api.nvim_win_get_cursor(w) end
  end
  vim.bo[m.buf].modifiable = true
  api.nvim_buf_set_lines(m.buf, 0, -1, false, msg.lines or {})
  vim.bo[m.buf].modifiable = false
  paint_map(m.buf)
  for w, c in pairs(keep) do pcall(api.nvim_win_set_cursor, w, { math.min(c[1], api.nvim_buf_line_count(m.buf)), c[2] }) end
  if msg.focus then
    S.focus_key = key
    show_focus_map()
  end
  refresh_bars()
end

--- The GRAPH pane: the memory graph around the image in focus (sent by the backend on each focus and request).
local function on_graph(msg)
  local b = S.bufs.graph
  local w = S.wins.graph
  local keep = valid_win(w) and api.nvim_win_get_buf(w) == b and S.graph_key == msg.key and api.nvim_win_get_cursor(w) or nil
  vim.bo[b].modifiable = true
  api.nvim_buf_set_lines(b, 0, -1, false, msg.lines or GRAPH_NONE)
  vim.bo[b].modifiable = false
  paint_map(b)
  S.graph_key, S.graph_title = msg.key, msg.title
  if valid_win(w) then pcall(api.nvim_win_set_cursor, w, keep and { math.min(keep[1], api.nvim_buf_line_count(b)), 0 } or { 1, 0 }) end
  refresh_bars()
end

--- The user touched element <id> on the pad: its line in the TREE, under the cursor of the panes showing the map.
local function on_map_hit(msg)
  local m = S.maps[msg.key]
  if not m then return end
  local lines = api.nvim_buf_get_lines(m.buf, 0, -1, false)
  local from = 1
  for i, l in ipairs(lines) do
    if l:match("^TREE") then from = i end
  end
  for i = from, #lines do
    if lines[i]:find("%f[%w]" .. vim.pesc(msg.id) .. "%f[^%w%.]") then
      api.nvim_buf_clear_namespace(m.buf, NS_HIT, 0, -1)
      pcall(api.nvim_buf_set_extmark, m.buf, NS_HIT, i - 1, 0, { line_hl_group = "DispMapHit", priority = 95 })
      for _, w in ipairs(api.nvim_list_wins()) do
        if api.nvim_win_get_buf(w) == m.buf then
          pcall(api.nvim_win_set_cursor, w, { i, 0 })
          api.nvim_win_call(w, function() vim.cmd("normal! zz") end)
        end
      end
      return
    end
  end
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
  for _, key in ipairs({ "map", "graph", "chat", "log", "iface" }) do
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

--- Go to the disp tab (building what is missing), on the input line.
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

--- Back to the tab you came from.
function M.back()
  if S and S.prev_tab and api.nvim_tabpage_is_valid(S.prev_tab) and S.prev_tab ~= S.tab then
    vim.cmd("stopinsert")
    api.nvim_set_current_tabpage(S.prev_tab)
    if valid_win(S.anchor) and api.nvim_win_get_tabpage(S.anchor) == S.prev_tab then pcall(api.nvim_set_current_win, S.anchor) end
  end
end

--- :Disp / dispi / dispk: start, else go to the disp tab, else (already in it) back to your tab.
function M.toggle()
  if not S then return M.start() end
  if in_tab() then return M.back() end
  M.show()
end
M.toggle_view = M.toggle

--- :DispMap [n]: bring image #n into focus (its MAP and GRAPH follow); without n, go to the MAP pane.
function M.map_pane(n)
  if not S then return end
  if n and n ~= "" then
    return send({ t = "focus", image = n })
  end
  if not in_tab() then M.show() end
  if valid_win(S.wins.map) then
    vim.cmd("stopinsert")
    api.nvim_set_current_win(S.wins.map)
  end
end

-- ---------------------------------------------------------------- MAP / GRAPH pane keys
local function common_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
  vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
  vim.keymap.set("n", "q", function() M.back() end, o)
  vim.keymap.set("n", "i", function() M.focus("chat") end, o)
end

function M._map_keys(b, key)
  local o = { buffer = b, silent = true, nowait = true }
  local function go(op)
    return function()
      local m = key and S.maps[key]
      if not m then return end
      local needs_id = op ~= "build" and op ~= "grid"
      local id = needs_id and element_at_cursor() or nil
      if needs_id and not id then return notify("no element on this line") end
      send({ t = "map_go", image = m.n, id = id, op = op })
    end
  end
  vim.keymap.set("n", "<CR>", go("show"), o)
  vim.keymap.set("n", "m", go("mark"), o)
  vim.keymap.set("n", "M", go("build"), o)
  vim.keymap.set("n", "gr", go("grid"), o)
  vim.keymap.set("n", "r", function() if key and S.maps[key] then send({ t = "map", image = S.maps[key].n }) end end, o)
  common_keys(b)
end

function M._graph_keys(b)
  local o = { buffer = b, silent = true, nowait = true }
  vim.keymap.set("n", "<CR>", function() -- an image key on the line: bring it into focus
    local line = api.nvim_get_current_line()
    local k = fn.expand("<cWORD>"):match("(%x%x%x%x#%d+)") or line:match("(%x%x%x%x#%d+)")
    if k then send({ t = "focus", image = k }) else notify("no image key (3fa2#3) on this line") end
  end, o)
  vim.keymap.set("n", "r", function() send({ t = "graph_refresh" }) end, o)
  common_keys(b)
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
  if not valid_win(win) then return { buffer = "", line = "" } end
  local buf = api.nvim_win_get_buf(win)
  local row = api.nvim_win_get_cursor(win)[1]
  local line = api.nvim_buf_get_lines(buf, row - 1, row, false)[1] or ""
  return { buffer = api.nvim_buf_get_name(buf), line = line, image = image_under_cursor(win) }
end

local OPS = {}
function OPS.context() return context() end

function OPS.ex(p)
  local win = anchor_win() or api.nvim_get_current_win()
  return api.nvim_win_call(win, function() return api.nvim_exec2(p.command, { output = true }).output end)
end

function OPS.lua(p)
  local chunk, err = load(p.code, "disp-agent", "t")
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

--- Text from an image into the editor: below the cursor of the window you came from, or a new scratch buffer
--- (split in your tab; you stay where you are).
function OPS.insert(p)
  local lines = vim.split(p.text or "", "\n", { plain = true })
  local win = anchor_win()
  if p.where == "new" then
    local made
    local function mk()
      vim.cmd("new")
      local buf = api.nvim_get_current_buf()
      vim.bo[buf].buftype, vim.bo[buf].bufhidden, vim.bo[buf].swapfile = "nofile", "hide", false
      pcall(api.nvim_buf_set_name, buf, "disp://text-" .. os.date("%H%M%S"))
      api.nvim_buf_set_lines(buf, 0, -1, false, lines)
      made = buf
    end
    if win then api.nvim_win_call(win, mk) else vim.cmd("tabnew"); mk() end
    return ("%d lines in a new buffer (%s)"):format(#lines, api.nvim_buf_get_name(made))
  end
  if not win then error("no editor window to insert into (go to your buffer, then ask again)") end
  local buf = api.nvim_win_get_buf(win)
  if not vim.bo[buf].modifiable then error("the buffer under the cursor is not modifiable") end
  local row = api.nvim_win_get_cursor(win)[1]
  api.nvim_buf_set_lines(buf, row, row, false, lines)
  return ("%d lines inserted below line %d of %s"):format(#lines, row, fn.fnamemodify(api.nvim_buf_get_name(buf), ":t"))
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
  elseif msg.t == "map" then
    on_map(msg)
  elseif msg.t == "graph" then
    on_graph(msg)
  elseif msg.t == "map_hit" then
    on_map_hit(msg)
  elseif msg.t == "reset" then -- ==== / the pad's strip emptied: no image in view, the MAP row back to its placeholder
    S.focus_key = nil
    on_graph({ key = nil, title = nil, lines = GRAPH_NONE })
    if in_tab() then show_focus_map() end
    refresh_bars()
  elseif msg.t == "req" then
    handle_req(msg)
  elseif msg.t == "show" then
    M.show()
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
  append("chat", "disp stopped (exit " .. tostring(code) .. "). :Disp restarts it.",
    code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh()
end

-- ---------------------------------------------------------------- input
local function submit(text, ctx)
  text = vim.trim(text or "")
  if text == "" then return end
  if not (S and S.job) then return notify("not running (:Disp)", vim.log.levels.WARN) end
  send({ t = "input", text = text, ctx = ctx or context() })
end

local function create_buffers()
  local chat = api.nvim_create_buf(false, true)
  vim.bo[chat].buftype = "prompt"
  vim.bo[chat].bufhidden = "hide"
  vim.bo[chat].filetype = "dispchat"
  pcall(api.nvim_buf_set_name, chat, "disp://chat")
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
  local graph = api.nvim_create_buf(false, true) -- the GRAPH pane (filled by the backend)
  vim.bo[graph].bufhidden = "hide"
  vim.bo[graph].filetype = "dispgraph"
  pcall(api.nvim_buf_set_name, graph, "disp://graph")
  api.nvim_buf_set_lines(graph, 0, -1, false, GRAPH_NONE)
  vim.bo[graph].modifiable = false
  M._graph_keys(graph)
  for key, ft in pairs({ log = "displog", iface = "dispiface" }) do
    local b = api.nvim_create_buf(false, true)
    vim.bo[b].bufhidden = "hide"
    vim.bo[b].filetype = ft
    vim.bo[b].modifiable = false
    pcall(api.nvim_buf_set_name, b, "disp://" .. (key == "log" and "llm" or key))
    bufs[key] = b
  end
  bufs.graph = graph
  for key, b in pairs(bufs) do
    if key == "graph" then goto continue end
    local o = { buffer = b, silent = true, nowait = true }
    vim.keymap.set("n", "<Tab>", function() cycle(1) end, o)
    vim.keymap.set("n", "<S-Tab>", function() cycle(-1) end, o)
    vim.keymap.set("n", "q", function() M.back() end, o)
    vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
    vim.keymap.set("n", "i", function() M.focus("chat") end, o)
    if b ~= chat then vim.keymap.set("n", "a", function() M.focus("chat") end, o) end
    ::continue::
  end
  return bufs
end

-- ---------------------------------------------------------------- lifecycle
local function normalize_args(args)
  args = args or vim.g.disp_args or {}
  if type(args) == "string" then args = vim.split(args, "%s+", { trimempty = true }) end
  return vim.deepcopy(args)
end

function M.start(args, opts)
  opts = opts or {}
  if S and S.job then return M.toggle() end
  local script = SCRIPT_DIR .. "/disp.py"
  args = normalize_args(args)
  -- On Windows "python3" is often the Microsoft Store stub; prefer "python" there (as memphia does).
  local py = vim.g.disp_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", script }
  local pad_bin = vim.g.disp_pad_bin and native(fn.fnamemodify(fn.expand(vim.g.disp_pad_bin), ":p")) or PAD_BIN
  vim.list_extend(cmd, { "--pad-bin", pad_bin })
  if vim.g.disp_memory then vim.list_extend(cmd, { "--memory", native(fn.fnamemodify(fn.expand(vim.g.disp_memory), ":p")) }) end
  if vim.g.disp_pad_port then vim.list_extend(cmd, { "--pad-port", tostring(vim.g.disp_pad_port) }) end
  vim.list_extend(cmd, args)
  local old = S
  S = { bufs = old and old.bufs or create_buffers(), wins = old and old.wins or {},
    maps = old and old.maps or {}, tab = old and old.tab, state = {},
    prev_tab = old and old.prev_tab or api.nvim_get_current_tabpage(), anchor = old and old.anchor or api.nvim_get_current_win() }
  if not in_tab() then S.prev_tab, S.anchor = api.nvim_get_current_tabpage(), api.nvim_get_current_win() end
  -- tell the backend where the inet driver lives, as a clean native path (Git Bash: /c/.. -> C:/..)
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = LOAD_CWD, env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
    on_stdout = on_stdout, on_stderr = on_stderr, on_exit = on_exit })
  if S.job <= 0 then
    S.job = nil
    return notify("cannot start " .. table.concat(cmd, " "), vim.log.levels.ERROR)
  end
  if fn.executable(pad_bin) ~= 1 then
    notify("the pad is not built yet: :DispBuild (cargo build --release in " .. SCRIPT_DIR .. "/pad)", vim.log.levels.WARN)
  end
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

--- :DispOpen [path]: an image to the pad (no LLM). Without a path: the image under the cursor, else ask.
function M.open(path)
  local win = anchor_win() or api.nvim_get_current_win()
  local function go(p)
    if not p or p == "" then return end
    p = native(fn.fnamemodify(fn.expand(p), ":p"))
    if fn.filereadable(p) ~= 1 then return notify("no such file: " .. p, vim.log.levels.WARN) end
    if ensure_started() then send({ t = "open", path = p }) end
  end
  if path and path ~= "" then return go(path) end
  local p = image_under_cursor(win)
  if p then return go(p) end
  vim.ui.input({ prompt = "disp: image to show: ", completion = "file" }, go)
end

function M.paste() if ensure_started() then send({ t = "paste" }) end end
function M.pad(op) if ensure_started() then send({ t = "pad", op = op or "raise" }) end end
function M.focus_image(n) if ensure_started() then send({ t = "focus", image = n }) end end
function M.map_build()
  if not S then return end
  local m = S.focus_key and S.maps[S.focus_key]
  if m then send({ t = "map_go", image = m.n, op = "build" }) end
end

--- :DispBuild: compile the pad in a terminal tab (cargo from PATH, else ~/.cargo/bin).
function M.build()
  local cargo = fn.executable("cargo") == 1 and "cargo" or fn.expand("~/.cargo/bin/cargo")
  if fn.executable(cargo) ~= 1 then
    return notify("cargo not found: install Rust (https://rustup.rs), then :DispBuild", vim.log.levels.ERROR)
  end
  vim.cmd("tabnew")
  fn.termopen({ cargo, "build", "--release" }, { cwd = SCRIPT_DIR .. "/pad", on_exit = function(_, code)
    vim.schedule(function()
      notify(code == 0 and ("pad built: " .. PAD_BIN) or ("pad build failed (exit " .. code .. ")"),
        code == 0 and vim.log.levels.INFO or vim.log.levels.ERROR)
    end)
  end })
end

function M.quit()
  if not S then return end
  if S.job then
    send({ t = "quit" })
    local job = S.job
    vim.defer_fn(function() pcall(fn.jobstop, job) end, 1500)
  end
  if S.tab and api.nvim_tabpage_is_valid(S.tab) and #api.nvim_list_tabpages() > 1 then
    M.back()
    pcall(function() vim.cmd("tabclose " .. api.nvim_tabpage_get_number(S.tab)) end)
  end
  local bufs = vim.tbl_values(S.bufs)
  for _, m in pairs(S.maps) do bufs[#bufs + 1] = m.buf end
  for _, b in ipairs(bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  S = nil
end

function M.status() return S and S.job and S.state.net or nil end
function M.running() return S ~= nil and S.job ~= nil end
M.image_under_cursor = image_under_cursor
M._session = function() return S end

local cmd = api.nvim_create_user_command
cmd("Disp", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start disp / go to its tab / back" })
cmd("DispToggle", function() M.toggle() end, { desc = "Go to the disp tab / back" })
cmd("DispAsk", function(o) M.ask(o.args) end, { nargs = "+", desc = "Ask disp" })
cmd("DispOpen", function(o) M.open(o.args) end, { nargs = "?", complete = "file", desc = "Show an image on DISPAD" })
cmd("DispPaste", function() M.paste() end, { desc = "The clipboard image to DISPAD" })
cmd("DispFocus", function(o) M.focus_image(o.args) end, { nargs = 1, desc = "Bring image #n to the front of DISPAD" })
cmd("DispMap", function(o) M.map_pane(o.args) end, { nargs = "?", desc = "Bring image #n into focus (MAP, GRAPH follow); no n: go to MAP" })
cmd("DispMapBuild", function() M.map_build() end, { desc = "Map the image in focus again" })
cmd("DispPad", function() M.pad("raise") end, { desc = "Show / start DISPAD" })
cmd("DispPadQuit", function() M.pad("quit") end, { desc = "Close DISPAD" })
cmd("DispBuild", function() M.build() end, { desc = "Build DISPAD (cargo build --release)" })
cmd("DispCancel", function() send({ t = "cancel" }) end, {})
cmd("DispQuit", M.quit, {})

if vim.g.disp_toggle_key then
  vim.keymap.set({ "n", "i", "t" }, vim.g.disp_toggle_key, function()
    if fn.mode() == "t" or fn.mode() == "i" then vim.cmd("stopinsert") end
    M.toggle()
  end, { silent = true, desc = "disp tab: go / back" })
end

local aug = api.nvim_create_augroup("disp", { clear = true })
api.nvim_create_autocmd("VimResized", { group = aug, callback = function() if in_tab() then sizes() end end })
api.nvim_create_autocmd("TabEnter", { group = aug, callback = function() if in_tab() then layout() end end })
api.nvim_create_autocmd("TabClosed", { group = aug, callback = function()
  if S and S.tab and not api.nvim_tabpage_is_valid(S.tab) then S.tab, S.wins = nil, {} end
end })
api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function() if S and S.job then send({ t = "quit" }) end end })
-- Leaving the input for another pane (mouse, <C-w>) leaves insert mode: MAP, LLM and IFACE are read-only.
api.nvim_create_autocmd("WinEnter", { group = aug, callback = function()
  if S and in_tab() and api.nvim_get_current_win() ~= S.wins.chat and fn.mode() == "i" then vim.cmd("stopinsert") end
end })
api.nvim_create_autocmd("WinClosed", { group = aug, callback = function(ev)
  if not S then return end
  local w = tonumber(ev.match)
  for key, x in pairs(S.wins) do if x == w then S.wins[key] = nil end end
end })

package.loaded.disp = M

-- ---------------------------------------------------------------- Intelligence Network
do
  local dir = vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet")
  local ok, inet = pcall(function() return package.loaded.inet or dofile(dir .. "/inet.lua") end)
  if ok and type(inet) == "table" then
    inet.register_agent("disp", {
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
    vim.notify("disp: Intelligence Network driver not loaded (" .. dir .. "/inet.lua): " .. tostring(inet),
      vim.log.levels.WARN)
  end
end

-- `:source disp.lua` (disp1) starts the agent and opens its tab, unless g:disp_no_autostart is set.
if MODNAME == nil and not vim.g.disp_no_autostart then M.start() end

return M
