-- knbg.lua: Neovim frontend of knbg, the Kanborg operator agent (requires Neovim >= 0.10).
-- Loaded only through ~/echo-home/.vim/mimd/vimpage_knbg; the kanborg tool itself knows nothing about it.
--
-- The agent (knbg.py, same folder) operates the kanborg tool on behalf of the user: project manager of the
-- board, Vim hands in this editor, coordinator on the Intelligence Network. It reaches the board only through
-- the tool's CLI. When the kanborg TUI module is loaded in this Neovim, its public Lua API (context / refresh /
-- view / open) is used, guarded by pcall; otherwise knbg shows views in its own scratch tab.
--
--   :Knbg [args]       start (backend args from g:knbg_args) / show / focus / hide the float
--   :KnbgAsk <text>    send a request without opening the float
--   :KnbgCancel        cancel the running task            :KnbgQuit  stop the backend
--   :InetJoin knbg / :InetLeave knbg    (profile g:inet_agents.knbg in vimpage_inet)
--
-- Options: g:knbg_python ("python"), g:knbg_args (list), g:knbg_root (kanborg tool folder,
--          default ~/echo-home/codev/kanborg), g:knbg_width (0.42), g:knbg_no_autostart.
-- In the float: last line is the input · <CR> send · <C-c> cancel task · F8 chat ⇄ IFACE · q hide (normal)

local MODNAME = ...
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
local PROMPT = "KanBorg ▶ "
local S -- session; nil when not running

-- The prompt label: dark background, light text. Override with  :hi KnbgPrompt ...  (defined as default).
api.nvim_set_hl(0, "KnbgPrompt", { default = true, bg = "#262626", fg = "#e4e4e4", bold = true,
  ctermbg = 235, ctermfg = 254, cterm = { bold = true } })
api.nvim_create_autocmd("ColorScheme", { callback = function()
  api.nvim_set_hl(0, "KnbgPrompt", { default = true, bg = "#262626", fg = "#e4e4e4", bold = true,
    ctermbg = 235, ctermfg = 254, cterm = { bold = true } })
end })
-- Painted at draw time on the chat buffer's input line, so it follows the prompt as the transcript grows.
local NS_PROMPT = api.nvim_create_namespace("knbg_prompt")
api.nvim_set_decoration_provider(NS_PROMPT, {
  on_win = function(_, _, buf) return S ~= nil and S.bufs ~= nil and buf == S.bufs.chat end,
  on_line = function(_, _, buf, row)
    if row ~= api.nvim_buf_line_count(buf) - 1 then return end
    local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
    if line:sub(1, #PROMPT) == PROMPT then
      api.nvim_buf_set_extmark(buf, NS_PROMPT, row, 0, { end_col = #PROMPT, hl_group = "KnbgPrompt",
        ephemeral = true, priority = 200 })
    end
  end,
})

local function notify(msg, level) vim.notify("knbg: " .. msg, level or vim.log.levels.INFO) end

local function root()
  return native(fn.fnamemodify(fn.expand(vim.g.knbg_root or "~/echo-home/codev/kanborg"), ":p"):gsub("[/\\]$", ""))
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

-- ---------------------------------------------------------------- buffers
local function append(key, text, hl)
  if not S then return end
  local buf = S.bufs[key]
  if not api.nvim_buf_is_valid(buf) then return end
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
  if valid_win(S.win) and api.nvim_win_get_buf(S.win) == buf and api.nvim_get_current_win() ~= S.win then
    api.nvim_win_set_cursor(S.win, { api.nvim_buf_line_count(buf), 0 })
  end
end

local function footer()
  local st = S and S.state or {}
  local parts = {}
  parts[#parts + 1] = st.busy and ("● " .. tostring(st.phase or "busy")) or "○ idle"
  if (st.queue or 0) > 0 then parts[#parts + 1] = st.queue .. " queued" end
  if st.auto then parts[#parts + 1] = "AUTO" end
  local net = st.net or {}
  if net.joined then
    parts[#parts + 1] = "🌐 @" .. tostring(net.name) .. " " .. (net.connected and ("on " .. tostring(net.network)) or "connecting")
      .. " [" .. tostring(net.state) .. "]"
  else
    parts[#parts + 1] = "🌐 off"
  end
  if st.approval then parts[#parts + 1] = "❓ y/n/a" end
  return " " .. table.concat(parts, " · ") .. " "
end

local function float_config()
  local cols, lines = vim.o.columns, vim.o.lines
  local w = math.max(40, math.floor(cols * (vim.g.knbg_width or 0.42)))
  local h = math.max(10, lines - 4)
  local title = S.pane == "iface" and " knbg · IFACE (F8 chat) " or " knbg · Kanborg operator (F8 network) "
  return { relative = "editor", row = 1, col = cols - w - 2, width = w, height = h, border = "rounded",
    title = title, title_pos = "center", footer = footer(), footer_pos = "left", zindex = 60 }
end

local function refresh_float()
  if S and valid_win(S.win) then pcall(api.nvim_win_set_config, S.win, float_config()) end
end

-- ---------------------------------------------------------------- float
local function float_here()
  return S and valid_win(S.win) and api.nvim_win_get_tabpage(S.win) == api.nvim_get_current_tabpage()
end

function M.show(enter)
  if not S then return M.start() end
  local cur = api.nvim_get_current_win()
  if cur ~= S.win then S.anchor = cur end
  if valid_win(S.win) and not float_here() then pcall(api.nvim_win_close, S.win, true) end
  if not float_here() then
    S.win = api.nvim_open_win(S.bufs[S.pane], enter ~= false, float_config())
    vim.wo[S.win].wrap, vim.wo[S.win].number, vim.wo[S.win].cursorline = true, false, false
    vim.wo[S.win].winhighlight = "NormalFloat:Normal"
  elseif enter ~= false then
    api.nvim_set_current_win(S.win)
  end
  api.nvim_win_set_cursor(S.win, { api.nvim_buf_line_count(S.bufs[S.pane]), 0 })
  if enter ~= false and S.pane == "chat" then vim.cmd("startinsert!") end
end

function M.hide()
  if S and valid_win(S.win) then
    pcall(api.nvim_win_close, S.win, true)
    S.win = nil
    if valid_win(S.anchor) then pcall(api.nvim_set_current_win, S.anchor) end
  end
end

function M.toggle()
  if not S then return M.start() end
  if float_here() and api.nvim_get_current_win() == S.win then return M.hide() end
  M.show(true)
end

local function switch_pane()
  S.pane = S.pane == "chat" and "iface" or "chat"
  if float_here() then
    api.nvim_win_set_buf(S.win, S.bufs[S.pane])
    refresh_float()
    api.nvim_win_set_cursor(S.win, { api.nvim_buf_line_count(S.bufs[S.pane]), 0 })
    if S.pane == "chat" then vim.cmd("startinsert!") else vim.cmd("stopinsert") end
  end
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

local function reshow()
  if S and S.win ~= nil then M.show(false) end
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
  local shown = S.win and valid_win(S.win)
  if valid_win(S.anchor) then api.nvim_set_current_win(S.anchor) end
  local ok = tui_call("view", p.kind, p.codes or {})
  if not ok then scratch_view(p.kind .. (#(p.codes or {}) > 0 and (" " .. table.concat(p.codes, ",")) or ""), p.text) end
  S.anchor = api.nvim_get_current_win()
  if shown then reshow() end
  return ok and "shown in the kanborg TUI" or "shown in a knbg tab"
end

function OPS.open(p)
  local shown = S.win and valid_win(S.win)
  if not tui_call("open", p.card) then
    if not p.path or p.path == "" then error("no path for " .. tostring(p.card)) end
    vim.cmd("tabnew " .. fn.fnameescape(p.path) .. " | setlocal filetype=markdown")
  end
  S.anchor = api.nvim_get_current_win()
  if shown then reshow() end
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
    append(msg.b == "iface" and "iface" or "chat", msg.text, msg.hl ~= vim.NIL and msg.hl or nil)
  elseif msg.t == "state" then
    S.state = msg
    refresh_float()
  elseif msg.t == "req" then
    handle_req(msg)
  elseif msg.t == "show" then
    if S.pane ~= "chat" then switch_pane() end
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
        if not ok2 then append("chat", "frontend error: " .. tostring(err), "ErrorMsg") end
      end
    end
  end
end

local function on_stderr(_, data)
  if not S then return end
  for _, l in ipairs(data) do
    if l ~= "" then append("iface", l, "Comment") end
  end
end

local function on_exit(_, code)
  if not S then return end
  S.job = nil
  append("chat", "knbg stopped (exit " .. tostring(code) .. "). :Knbg restarts it.",
    code == 0 and "Comment" or "ErrorMsg")
  S.state = {}
  refresh_float()
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
  local iface = api.nvim_create_buf(false, true)
  vim.bo[iface].bufhidden = "hide"
  vim.bo[iface].filetype = "knbgiface"
  pcall(api.nvim_buf_set_name, iface, "knbg://iface")
  for _, b in ipairs({ chat, iface }) do
    local o = { buffer = b, silent = true, nowait = true }
    vim.keymap.set({ "n", "i" }, "<F8>", switch_pane, o)
    vim.keymap.set("n", "q", M.hide, o)
    vim.keymap.set("n", "<C-c>", function() send({ t = "cancel" }) end, o)
    vim.keymap.set("n", "i", function()
      if S.pane ~= "chat" then switch_pane() else vim.cmd("startinsert!") end
    end, o)
  end
  return { chat = chat, iface = iface }
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
  local has_root = vim.tbl_contains(args, "--root")
  -- On Windows "python3" is often the Microsoft Store stub; prefer "python" there (as memphia does).
  local py = vim.g.knbg_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local cmd = { py, "-u", script }
  if not has_root then vim.list_extend(cmd, { "--root", root() }) end
  vim.list_extend(cmd, args)
  local bufs = S and S.bufs or create_buffers()
  S = { bufs = bufs, pane = "chat", state = {}, anchor = api.nvim_get_current_win() }
  -- tell the backend where the inet driver lives, as a clean native path (Git Bash: /c/.. -> C:/..)
  local inet_dir = native(vim.g.inet_dir and fn.expand(vim.g.inet_dir) or (fn.fnamemodify(SCRIPT_DIR, ":h") .. "/inet"))
  S.job = fn.jobstart(cmd, { cwd = root(), env = { INET_DIR = inet_dir, PYTHONIOENCODING = "utf-8" },
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

local cmd = api.nvim_create_user_command
cmd("Knbg", function(o)
  if #o.fargs > 0 and not (S and S.job) then return M.start(o.fargs) end
  M.toggle()
end, { nargs = "*", desc = "Start / show / hide knbg, the Kanborg operator" })
cmd("KnbgAsk", function(o) M.ask(o.args) end, { nargs = "+", desc = "Ask knbg" })
cmd("KnbgCancel", function() send({ t = "cancel" }) end, {})
cmd("KnbgQuit", M.quit, {})

api.nvim_create_autocmd("VimResized", { callback = refresh_float })
api.nvim_create_autocmd("VimLeavePre", { callback = function() if S and S.job then send({ t = "quit" }) end end })

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

-- `:source knbg.lua` opens the agent unless g:knbg_no_autostart is set (vimpage_knbg sets it).
if MODNAME == nil and not vim.g.knbg_no_autostart then M.start() end

return M
