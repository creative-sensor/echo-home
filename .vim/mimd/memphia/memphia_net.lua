-- memphia_net.lua: the Intelligence Network tab — a Slack-like room where Memphia agents
-- (through their iFace) and humans meet. Requires Neovim >= 0.10.
--
--   :source /path/to/memphia_net.lua   open the room (memphia.lua also registers the commands)
--   :MemphiaNet [name] [host:port]     open/focus the room and join as a human
--   :MemphiaNetQuit                    leave and close the tab
--
-- The hub (memphia_hub.py) is started on demand when nothing listens on a local host:port.
-- Options: vim.g.memphia_net_name (default $USER), vim.g.memphia_net_hub ("127.0.0.1:7788"),
--          vim.g.memphia_net_watch (default true: also see agents' point-to-point exchanges, dimmed),
--          vim.g.memphia_python.
--
-- Speech: plain text is broadcast to the room. "@name" makes it point-to-point: only the named
-- members (and watchers) receive it. "@all" broadcasts and mentions everyone.
-- Input line: <Tab> completes @names; /who /clear /quit /help.
-- Roster: <CR> on a member starts a message to them.

local REGISTER_ONLY = rawget(_G, "__memphia_net_register_only")
do -- keep a running session when the file is sourced again
  local prev = package.loaded.memphia_net
  if type(prev) == "table" and prev.open then
    if not REGISTER_ONLY then prev.open() end
    return prev
  end
end

local M = {}
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

local SCRIPT_DIR = fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
if fn.has("win32") == 1 then
  local drive, rest = SCRIPT_DIR:match("^[/\\]([a-zA-Z])[/\\](.*)$")
  if drive and fn.isdirectory(SCRIPT_DIR) == 0 then SCRIPT_DIR = drive:upper() .. ":/" .. rest end
  SCRIPT_DIR = SCRIPT_DIR:gsub("\\", "/")
end

local NS = api.nvim_create_namespace("memphia_net")
local NS_PROMPT = api.nvim_create_namespace("memphia_net_prompt")
local MAX_LINES = 5000
local ROSTER_WIDTH = 34
local PALETTE = { "#5fafff", "#87d787", "#ff875f", "#d787ff", "#5fd7d7", "#ffaf5f", "#ff5f87", "#afd75f",
  "#87afff", "#d7af87" }
local STATE_ICON = { deliberating = "🤔", thinking = "💭", listening = "👂", offline = "✖" }
local HELP = "plain text → room │ @name → point-to-point │ @all → everyone │ <Tab> complete @name │ "
  .. "/who /clear /quit"

local N -- session; nil when the room is closed

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    MemphiaNetHeader = { bg = "#1c1c1c", fg = "#888888" },
    MemphiaNetTitle = { bg = "#005f5f", fg = "#ffffff", bold = true },
    MemphiaNetOff = { bg = "#af0000", fg = "#ffffff", bold = true },
    MemphiaNetTime = { fg = "#6c6c6c" },
    MemphiaNetSys = { fg = "#808080", italic = true },
    MemphiaNetErr = { fg = "#ff5f5f", italic = true },
    MemphiaNetAt = { fg = "#87afd7", bold = true },
    MemphiaNetMention = { bg = "#5f5f00", fg = "#ffff87", bold = true },
    MemphiaNetWhisper = { fg = "#af87d7", italic = true },
    MemphiaNetDim = { fg = "#808080" },
    MemphiaNetPrompt = { bg = "#005f5f", fg = "#ffffff", bold = true },
    MemphiaNetHint = { fg = "#808080" },
  }
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
  for i, c in ipairs(PALETTE) do api.nvim_set_hl(0, "MemphiaNetName" .. i, { fg = c, bold = true, default = true }) end
end

local function name_hl(name)
  local h = 0
  for i = 1, #name do h = (h * 31 + name:byte(i)) % 1000003 end
  return "MemphiaNetName" .. (h % #PALETTE + 1)
end

-- ---------------------------------------------------------------- helpers
local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end
local function is_local(host) return host == "127.0.0.1" or host == "localhost" or host == "::1" end
local function glyph(kind) return kind == "agent" and "◆" or "●" end

local function send(msg)
  if N and N.tcp and not N.tcp:is_closing() then N.tcp:write(vim.json.encode(msg) .. "\n") end
end

local function resolve_host(host)
  if host == "localhost" then return "127.0.0.1" end
  if host:match("^%d+%.%d+%.%d+%.%d+$") then return host end
  local res = uv.getaddrinfo(host, nil, { family = "inet", socktype = "stream" })
  return res and res[1] and res[1].addr or host
end

-- ---------------------------------------------------------------- bars
local function refresh_bars()
  if not N then return end
  local w = N.wins
  if valid_win(w.chat) then
    local n = vim.tbl_count(N.members)
    vim.wo[w.chat].winbar = "%#MemphiaNetTitle# 🌐 INTELLIGENCE NETWORK %#MemphiaNetHeader# "
      .. esc(N.host) .. ":" .. N.port .. " │ you: @" .. esc(N.name) .. " │ " .. n .. " member" .. (n == 1 and "" or "s")
      .. (N.watch and " │ watching all traffic" or "")
      .. (N.connected and "" or " %#MemphiaNetOff# offline %#MemphiaNetHeader#")
      .. (N.follow and "" or "  ⏸ scrolled")
  end
  if valid_win(w.roster) then vim.wo[w.roster].winbar = "%#MemphiaNetHeader# MEMBERS" end
end

-- ---------------------------------------------------------------- chat buffer
local function chat_append(lines, hls)
  local buf = N.bufs.chat
  vim.bo[buf].modifiable = true
  local n = api.nvim_buf_line_count(buf)
  local empty = n == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == ""
  local start = empty and 0 or n
  api.nvim_buf_set_lines(buf, start, empty and 1 or n, false, lines)
  for _, h in ipairs(hls) do
    pcall(api.nvim_buf_set_extmark, buf, NS, start + h[1], h[2],
      { end_col = h[3], hl_group = h[4], priority = h[5] or 100 })
  end
  local total = api.nvim_buf_line_count(buf)
  if total > MAX_LINES then
    api.nvim_buf_set_lines(buf, 0, total - MAX_LINES, false, {})
    total = MAX_LINES
  end
  vim.bo[buf].modifiable = false
  if N.follow then
    for _, win in ipairs(fn.win_findbuf(buf)) do api.nvim_win_set_cursor(win, { total, 0 }) end
  end
end

local function sys(text, hl)
  local line = "      ── " .. text .. " ──"
  chat_append({ line }, { { 0, 0, #line, hl or "MemphiaNetSys" } })
end

local function render_msg(m)
  local t = os.date("%H:%M", math.floor(tonumber(m.ts) or os.time()))
  local from = tostring(m.from or "?")
  local to = m.to or {}
  local whisper = #to > 0
  local head = t .. " " .. glyph(m.kind) .. " " .. from
  local name_s, name_e = #t + 1, #head
  local arrow = ""
  if whisper then
    local names = {}
    for _, x in ipairs(to) do names[#names + 1] = "@" .. x end
    arrow = " → " .. table.concat(names, " ")
  end
  head = head .. arrow .. "  "
  local indent = string.rep(" ", fn.strdisplaywidth(t) + 3)
  local lines, hls = {}, {}
  for i, l in ipairs(vim.split(tostring(m.text or ""), "\n", { plain = true })) do
    local prefix = i == 1 and head or indent
    lines[#lines + 1] = prefix .. l
    local row = #lines - 1
    if i == 1 then
      hls[#hls + 1] = { row, 0, #t, "MemphiaNetTime" }
      hls[#hls + 1] = { row, name_s, name_e, name_hl(from) }
      if whisper then hls[#hls + 1] = { row, name_e, name_e + #arrow, "MemphiaNetWhisper" } end
    end
    if whisper then hls[#hls + 1] = { row, #prefix, #prefix + #l, "MemphiaNetWhisper" } end
    local s = 1
    while true do
      local a, b, who = l:find("@([%w_%.%-]+)", s)
      if not a then break end
      local low = who:lower()
      local mine = low == N.name:lower() or low == "all" or low == "here" or low == "everyone"
      hls[#hls + 1] = { row, #prefix + a - 1, #prefix + b, mine and "MemphiaNetMention" or "MemphiaNetAt", 200 }
      s = b + 1
    end
  end
  chat_append(lines, hls)
end

-- ---------------------------------------------------------------- roster
local function render_roster()
  local rb = N.bufs.roster
  local names = vim.tbl_keys(N.members)
  table.sort(names, function(a, b)
    local ka, kb = N.members[a].kind, N.members[b].kind
    if ka ~= kb then return ka == "human" end
    return a:lower() < b:lower()
  end)
  local lines, hls = {}, {}
  N.roster_rows = {}
  for _, name in ipairs(names) do
    local m = N.members[name]
    local g = glyph(m.kind)
    local line = " " .. g .. " " .. name .. (name == N.name and " (you)" or "")
    hls[#hls + 1] = { #lines, 1, 1 + #g + 1 + #name, name_hl(name) }
    if m.kind == "agent" and m.state and m.state ~= "" then
      local s = #line
      line = line .. "  " .. (STATE_ICON[m.state] or "·") .. " " .. m.state
      hls[#hls + 1] = { #lines, s, #line, "MemphiaNetDim" }
    end
    lines[#lines + 1] = line
    N.roster_rows[#lines] = name
    if m.purpose and m.purpose ~= "" and m.kind == "agent" then
      local p = "   " .. tostring(m.purpose):gsub("%s+", " ")
      lines[#lines + 1] = p
      N.roster_rows[#lines] = name
      hls[#hls + 1] = { #lines - 1, 0, #p, "MemphiaNetSys" }
    end
  end
  vim.bo[rb].modifiable = true
  api.nvim_buf_set_lines(rb, 0, -1, false, lines)
  api.nvim_buf_clear_namespace(rb, NS, 0, -1)
  for _, h in ipairs(hls) do pcall(api.nvim_buf_set_extmark, rb, NS, h[1], h[2], { end_col = h[3], hl_group = h[4] }) end
  vim.bo[rb].modifiable = false
  refresh_bars()
end

-- ---------------------------------------------------------------- input line
local function update_prompt()
  local ib = N.bufs.input
  local text = " @" .. N.name .. " ❯ "
  local old = N.prompt
  if text ~= old then
    fn.prompt_setprompt(ib, text)
    local n = api.nvim_buf_line_count(ib)
    local line = api.nvim_buf_get_lines(ib, n - 1, n, false)[1]
    if old ~= "" and line:sub(1, #old) == old then
      api.nvim_buf_set_text(ib, n - 1, 0, n - 1, #old, { text })
    elseif line:sub(1, #text) ~= text then
      api.nvim_buf_set_lines(ib, n - 1, n, false, { text .. line })
    end
    N.prompt = text
  end
  api.nvim_buf_clear_namespace(ib, NS_PROMPT, 0, -1)
  local n = api.nvim_buf_line_count(ib)
  local name_end = #text - #"❯ "
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, 0, { end_col = name_end, hl_group = "MemphiaNetPrompt" })
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, name_end, { end_col = #text, hl_group = "MemphiaNetHint" })
end

local function set_input(text)
  local ib = N.bufs.input
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_set_lines(ib, n - 1, n, false, { N.prompt .. text })
  update_prompt()
  if valid_win(N.wins.input) then
    api.nvim_set_current_win(N.wins.input)
    api.nvim_win_set_cursor(N.wins.input, { n, #N.prompt + #text })
    vim.cmd("startinsert!")
  end
end

local function on_submit(text)
  vim.schedule(function()
    if N and api.nvim_buf_is_valid(N.bufs.input) then
      api.nvim_buf_set_lines(N.bufs.input, 0, -2, false, {})
      update_prompt()
    end
  end)
  text = vim.trim(text or "")
  if text == "" then return end
  if text:sub(1, 1) == "/" then
    local cmd = text:match("^/(%S+)")
    if cmd == "who" then
      for name, m in pairs(N.members) do
        sys(glyph(m.kind) .. " @" .. name .. " [" .. tostring(m.state or "") .. "] " .. tostring(m.purpose or ""))
      end
    elseif cmd == "clear" then
      vim.bo[N.bufs.chat].modifiable = true
      api.nvim_buf_set_lines(N.bufs.chat, 0, -1, false, {})
      api.nvim_buf_clear_namespace(N.bufs.chat, NS, 0, -1)
      vim.bo[N.bufs.chat].modifiable = false
    elseif cmd == "quit" or cmd == "leave" then
      vim.schedule(M.quit)
    else
      sys(HELP)
    end
    return
  end
  if not N.connected then
    sys("not connected — :MemphiaNet to reconnect", "MemphiaNetErr")
    return
  end
  send({ t = "say", text = text })
end

-- <Tab> after "@par" completes member names; cycles the popup when it is open.
local function tab_complete()
  if fn.pumvisible() == 1 then return "<C-n>" end
  local col = api.nvim_win_get_cursor(0)[2]
  local before = api.nvim_get_current_line():sub(1, col)
  local partial = before:match("@([%w_%.%-]*)$")
  if not partial then return "<Tab>" end
  local items = {}
  for name in pairs(N.members) do
    if name ~= N.name and name:lower():sub(1, #partial) == partial:lower() then items[#items + 1] = name end
  end
  table.sort(items)
  if ("all"):sub(1, #partial) == partial:lower() then items[#items + 1] = "all" end
  if #items == 0 then return "" end
  local start = col - #partial + 1
  vim.schedule(function()
    if fn.mode() == "i" then fn.complete(start, vim.tbl_map(function(x) return x .. " " end, items)) end
  end)
  return ""
end

-- ---------------------------------------------------------------- hub protocol
local function on_message(msg)
  local t = msg.t
  if t == "msg" then
    render_msg(msg)
    local me = N.name:lower()
    local mine = false
    for _, x in ipairs(msg.mentions or {}) do if x:lower() == me then mine = true end end
    for _, x in ipairs(msg.to or {}) do if x:lower() == me then mine = true end end
    if mine and msg.from ~= N.name and api.nvim_get_current_tabpage() ~= N.tab then
      vim.notify("🌐 @" .. tostring(msg.from) .. ": " .. tostring(msg.text):sub(1, 200), vim.log.levels.INFO)
    end
  elseif t == "welcome" then
    N.name = msg.name or N.name
    N.connected = true
    N.members = {}
    for _, m in ipairs(msg.members or {}) do N.members[m.name] = m end
    if not N.seen_history then
      for _, h in ipairs(msg.history or {}) do render_msg(h) end
      N.seen_history = true
    end
    sys("joined " .. N.host .. ":" .. N.port .. " as @" .. N.name .. "   (" .. HELP .. ")")
    update_prompt()
    render_roster()
  elseif t == "members" then
    N.members = {}
    for _, m in ipairs(msg.members or {}) do N.members[m.name] = m end
    render_roster()
  elseif t == "status" then
    if N.members[msg.name] then
      N.members[msg.name].state = msg.state
      render_roster()
    end
  elseif t == "join" or t == "leave" then
    local m = msg.member or {}
    if m.name ~= N.name then
      local purpose = (m.kind == "agent" and m.purpose and m.purpose ~= "") and (": " .. m.purpose) or ""
      sys(glyph(m.kind) .. " @" .. tostring(m.name) .. (t == "join" and " joined" or " left") .. purpose)
    end
  elseif t == "error" then
    sys("hub: " .. tostring(msg.text), "MemphiaNetErr")
  end
end

local function disconnected(reason)
  if not N then return end
  if N.tcp and not N.tcp:is_closing() then N.tcp:close() end
  N.tcp = nil
  if N.connected then sys("disconnected" .. (reason or "") .. " — :MemphiaNet to reconnect", "MemphiaNetErr") end
  N.connected = false
  N.members = {}
  render_roster()
end

local function start_hub()
  if N.hub_job then return end
  local py = vim.g.memphia_python or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3")
  local job = fn.jobstart({ py, "-u", SCRIPT_DIR .. "/memphia_hub.py", "--host", resolve_host(N.host), "--port",
    tostring(N.port) }, {
    on_exit = function(_, code)
      if N and N.hub_job then
        N.hub_job = nil
        if code ~= 0 then sys("hub process exited with code " .. code, "MemphiaNetErr") end
      end
    end,
  })
  if job > 0 then N.hub_job = job end
end

local function connect(attempt)
  attempt = attempt or 1
  local tcp = uv.new_tcp()
  N.tcp, N.partial = tcp, ""
  tcp:connect(resolve_host(N.host), N.port, function(err)
    if err then
      tcp:close()
      vim.schedule(function()
        if not N or N.tcp ~= tcp then return end
        N.tcp = nil
        if is_local(N.host) and attempt < 15 then
          if attempt == 1 then
            sys("no hub on " .. N.host .. ":" .. N.port .. " — starting memphia_hub.py")
            start_hub()
          end
          vim.defer_fn(function() if N and not N.tcp then connect(attempt + 1) end end, 300)
        else
          sys("cannot reach the hub at " .. N.host .. ":" .. N.port .. " (" .. err .. ")", "MemphiaNetErr")
        end
      end)
      return
    end
    tcp:read_start(function(rerr, chunk)
      if rerr or not chunk then
        vim.schedule(function() if N and N.tcp == tcp then disconnected(rerr and (": " .. rerr) or "") end end)
        return
      end
      vim.schedule(function()
        if not N or N.tcp ~= tcp then return end
        local lines = vim.split(N.partial .. chunk, "\n", { plain = true })
        N.partial = table.remove(lines)
        for _, line in ipairs(lines) do
          if line ~= "" then
            local ok, msg = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
            if ok and type(msg) == "table" then on_message(msg) end
          end
        end
      end)
    end)
    vim.schedule(function()
      if N and N.tcp == tcp then send({ t = "hello", name = N.name, kind = "human", purpose = "human", watch = N.watch }) end
    end)
  end)
end

-- ---------------------------------------------------------------- layout
local function setup_view_win(win)
  local wo = vim.wo[win]
  wo.wrap, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.foldcolumn, wo.list, wo.spell, wo.cursorline = "0", false, false, false
  wo.linebreak, wo.breakindent = true, true
  wo.statusline = "%#MemphiaNetHeader#"
end

local function build_layout()
  local w = N.wins
  vim.cmd("tabnew")
  N.tab = api.nvim_get_current_tabpage()
  local scratch = api.nvim_get_current_buf()
  w.chat = api.nvim_get_current_win()
  api.nvim_win_set_buf(w.chat, N.bufs.chat)
  if api.nvim_buf_is_valid(scratch) and scratch ~= N.bufs.chat then pcall(api.nvim_buf_delete, scratch, { force = true }) end
  setup_view_win(w.chat)
  w.roster = api.nvim_open_win(N.bufs.roster, false, { split = "right", win = w.chat, width = ROSTER_WIDTH })
  setup_view_win(w.roster)
  vim.wo[w.roster].winfixwidth = true
  w.input = api.nvim_open_win(N.bufs.input, true, { split = "below", win = -1, height = 1 })
  local wo = vim.wo[w.input]
  wo.winfixheight, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.wrap, wo.cursorline, wo.foldcolumn = true, false, "0"
  wo.statusline = "%#MemphiaNetHint# " .. esc(HELP) .. " │ :MemphiaNetQuit"
  refresh_bars()
  render_roster()
  update_prompt()
  api.nvim_set_current_win(w.input)
  vim.cmd("startinsert!")
end

local function layout_alive()
  return N.tab and api.nvim_tabpage_is_valid(N.tab) and valid_win(N.wins.chat) and valid_win(N.wins.input)
end

local function create_buffers()
  local bufs = {}
  for _, key in ipairs({ "chat", "roster" }) do
    local b = api.nvim_create_buf(false, true)
    pcall(api.nvim_buf_set_name, b, "memphia://network" .. (key == "chat" and "" or "/" .. key))
    vim.bo[b].bufhidden, vim.bo[b].swapfile, vim.bo[b].modifiable = "hide", false, false
    vim.bo[b].filetype = "memphianet"
    bufs[key] = b
  end
  local ib = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, ib, "memphia://network/input")
  vim.bo[ib].buftype, vim.bo[ib].bufhidden, vim.bo[ib].swapfile = "prompt", "hide", false
  fn.prompt_setcallback(ib, on_submit)
  bufs.input = ib
  return bufs
end

local function set_keymaps()
  local o = { nowait = true, silent = true }
  local function map(modes, lhs, rhs, buf, extra)
    vim.keymap.set(modes, lhs, rhs, vim.tbl_extend("force", o, { buffer = buf }, extra or {}))
  end
  local ib, views = N.bufs.input, { N.bufs.chat, N.bufs.roster }
  map("i", "<Tab>", tab_complete, ib, { expr = true })
  map({ "n", "i" }, "<C-q>", M.quit, ib)
  local function scroll(keys)
    return function()
      if not valid_win(N.wins.chat) then return end
      api.nvim_win_call(N.wins.chat, function()
        vim.cmd("normal! " .. api.nvim_replace_termcodes(keys, true, false, true))
      end)
    end
  end
  map("i", "<PageUp>", scroll("<C-b>"), ib)
  map("i", "<PageDown>", scroll("<C-f>"), ib)
  map("i", "<S-Up>", scroll("<C-y>"), ib)
  map("i", "<S-Down>", scroll("<C-e>"), ib)
  local function to_input() set_input(api.nvim_buf_get_lines(ib, -2, -1, false)[1]:sub(#N.prompt + 1)) end
  for _, b in ipairs(views) do
    for _, lhs in ipairs({ "i", "a", "I", "A", "o" }) do map("n", lhs, to_input, b) end
    map("n", "<C-q>", M.quit, b)
  end
  map("n", "<CR>", to_input, N.bufs.chat)
  map("n", "<CR>", function()
    local name = N.roster_rows and N.roster_rows[api.nvim_win_get_cursor(0)[1]]
    if name and name ~= N.name then set_input("@" .. name .. " ") else to_input() end
  end, N.bufs.roster)
  map("n", "G", function()
    N.follow = true
    api.nvim_win_set_cursor(0, { api.nvim_buf_line_count(0), 0 })
    refresh_bars()
  end, N.bufs.chat)
end

local function set_autocmds()
  local group = api.nvim_create_augroup("MemphiaNet", { clear = true })
  api.nvim_create_autocmd("WinScrolled", {
    group = group,
    callback = function()
      if not N or not valid_win(N.wins.chat) then return end
      local f = api.nvim_win_call(N.wins.chat, function() return fn.line("w$") >= fn.line("$") end)
      if f ~= N.follow then
        N.follow = f
        refresh_bars()
      end
    end,
  })
  api.nvim_create_autocmd("VimLeavePre", { group = group, callback = function() if N then send({ t = "bye" }) end end })
end

-- ---------------------------------------------------------------- public API
function M.open(fargs)
  define_highlights()
  fargs = fargs or {}
  local name, hub
  for _, a in ipairs(fargs) do
    if a:match(":%d+$") then hub = a else name = a end
  end
  if N then
    if not layout_alive() then build_layout() else api.nvim_set_current_tabpage(N.tab) end
    if not N.tcp then connect() end
    return
  end
  hub = hub or vim.g.memphia_net_hub or "127.0.0.1:7788"
  local host, port = hub:match("^(.*):(%d+)$")
  local watch = vim.g.memphia_net_watch
  N = {
    name = name or vim.g.memphia_net_name or vim.env.USER or "human",
    host = (host and host ~= "") and host or "127.0.0.1", port = tonumber(port) or 7788,
    watch = watch == nil or (watch ~= false and watch ~= 0),
    members = {}, wins = {}, follow = true, prompt = "", partial = "",
  }
  N.bufs = create_buffers()
  set_keymaps()
  set_autocmds()
  build_layout()
  connect()
end

function M.quit()
  if not N then return end
  send({ t = "bye" })
  local tcp = N.tcp
  N.tcp = nil
  if tcp and not tcp:is_closing() then vim.defer_fn(function() if not tcp:is_closing() then tcp:close() end end, 100) end
  if N.tab and api.nvim_tabpage_is_valid(N.tab) and #api.nvim_list_tabpages() > 1 then
    pcall(vim.cmd, "tabclose " .. api.nvim_tabpage_get_number(N.tab))
  end
  for _, b in pairs(N.bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
  pcall(api.nvim_del_augroup_by_name, "MemphiaNet")
  N = nil -- the hub (if we started it) keeps running for the agents until nvim exits
end

api.nvim_create_user_command("MemphiaNet", function(o) M.open(o.fargs) end,
  { nargs = "*", desc = "Open the Intelligence Network room: [name] [host:port]" })
api.nvim_create_user_command("MemphiaNetQuit", M.quit, { desc = "Leave the Intelligence Network room" })

package.loaded.memphia_net = M

if not REGISTER_ONLY and not vim.g.memphia_net_no_autostart then M.open() end

return M
