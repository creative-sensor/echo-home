-- inet_room.lua: a network room as a Neovim tab — Slack-like chat where agents and humans meet.
-- Built on the inet.lua driver (I.client). One room (tab) per network. Open with :InetRoom.
--
-- Speech: plain text → the room; "@name" → point-to-point (only the named members, + watchers);
-- "@all" → everyone. Input line: <Tab> completes @names; /who /clear /quit /help.
-- Roster: <CR> on a member starts a message to them.

do
  local prev = package.loaded.inet_room
  if type(prev) == "table" and prev.open then return prev end
end

local I = package.loaded.inet
local R = {}
local api, fn = vim.api, vim.fn

local NS = api.nvim_create_namespace("inet_room")
local NS_PROMPT = api.nvim_create_namespace("inet_room_prompt")
local MAX_LINES = 5000
local ROSTER_WIDTH = 34
local PALETTE = { "#5fafff", "#87d787", "#ff875f", "#d787ff", "#5fd7d7", "#ffaf5f", "#ff5f87", "#afd75f",
  "#87afff", "#d7af87" }
local STATE_ICON = { deliberating = "🤔", thinking = "💭", listening = "👂", offline = "✖" }
local HELP = "plain text → room │ @name → point-to-point │ @all → everyone │ <Tab> complete @name │ /who /clear /quit"

local rooms = {} -- network name -> room

-- ---------------------------------------------------------------- highlights
local function define_highlights()
  local hl = {
    InetHeader = { bg = "#1c1c1c", fg = "#888888" },
    InetTitle = { bg = "#005f5f", fg = "#ffffff", bold = true },
    InetOff = { bg = "#af0000", fg = "#ffffff", bold = true },
    InetTime = { fg = "#6c6c6c" },
    InetSys = { fg = "#808080", italic = true },
    InetErr = { fg = "#ff5f5f", italic = true },
    InetAt = { fg = "#87afd7", bold = true },
    InetMention = { bg = "#5f5f00", fg = "#ffff87", bold = true },
    InetWhisper = { fg = "#af87d7", italic = true },
    InetDim = { fg = "#808080" },
    InetPrompt = { bg = "#005f5f", fg = "#ffffff", bold = true },
    InetHint = { fg = "#808080" },
  }
  for name, spec in pairs(hl) do
    spec.default = true
    api.nvim_set_hl(0, name, spec)
  end
  for i, c in ipairs(PALETTE) do api.nvim_set_hl(0, "InetName" .. i, { fg = c, bold = true, default = true }) end
end

local function name_hl(name)
  local h = 0
  for i = 1, #name do h = (h * 31 + name:byte(i)) % 1000003 end
  return "InetName" .. (h % #PALETTE + 1)
end

local function valid_win(w) return w and api.nvim_win_is_valid(w) end
local function esc(s) return (tostring(s or ""):gsub("%%", "%%%%")) end
local function glyph(kind) return kind == "agent" and "◆" or "●" end

-- ---------------------------------------------------------------- bars
local function refresh_bars(r)
  local w, c = r.wins, r.client
  if valid_win(w.chat) then
    local n = vim.tbl_count(c.members)
    vim.wo[w.chat].winbar = "%#InetTitle# 🌐 " .. esc(c.network):upper() .. " %#InetHeader# " .. esc(c:addr())
      .. " │ you: @" .. esc(c.name) .. " │ " .. n .. " member" .. (n == 1 and "" or "s")
      .. (c.o.watch and " │ watching all traffic" or "")
      .. (c.connected and "" or " %#InetOff# offline %#InetHeader#")
      .. (r.follow and "" or "  ⏸ scrolled")
  end
  if valid_win(w.roster) then vim.wo[w.roster].winbar = "%#InetHeader# MEMBERS" end
end

-- ---------------------------------------------------------------- chat buffer
local function chat_append(r, lines, hls)
  local buf = r.bufs.chat
  vim.bo[buf].modifiable = true
  local n = api.nvim_buf_line_count(buf)
  local empty = n == 1 and api.nvim_buf_get_lines(buf, 0, 1, false)[1] == ""
  local start = empty and 0 or n
  api.nvim_buf_set_lines(buf, start, empty and 1 or n, false, lines)
  for _, h in ipairs(hls) do
    pcall(api.nvim_buf_set_extmark, buf, NS, start + h[1], h[2], { end_col = h[3], hl_group = h[4], priority = h[5] or 100 })
  end
  local total = api.nvim_buf_line_count(buf)
  if total > MAX_LINES then
    api.nvim_buf_set_lines(buf, 0, total - MAX_LINES, false, {})
    total = MAX_LINES
  end
  vim.bo[buf].modifiable = false
  if r.follow then
    for _, win in ipairs(fn.win_findbuf(buf)) do api.nvim_win_set_cursor(win, { total, 0 }) end
  end
end

local function sys(r, text, hl)
  local line = "      ── " .. text .. " ──"
  chat_append(r, { line }, { { 0, 0, #line, hl or "InetSys" } })
end

local function render_msg(r, m)
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
  local me = r.client.name:lower()
  local lines, hls = {}, {}
  for i, l in ipairs(vim.split(tostring(m.text or ""), "\n", { plain = true })) do
    local prefix = i == 1 and head or indent
    lines[#lines + 1] = prefix .. l
    local row = #lines - 1
    if i == 1 then
      hls[#hls + 1] = { row, 0, #t, "InetTime" }
      hls[#hls + 1] = { row, name_s, name_e, name_hl(from) }
      if whisper then hls[#hls + 1] = { row, name_e, name_e + #arrow, "InetWhisper" } end
    end
    if whisper then hls[#hls + 1] = { row, #prefix, #prefix + #l, "InetWhisper" } end
    local s = 1
    while true do
      local a, b, who = l:find("@([%w_%.%-]+)", s)
      if not a then break end
      local low = who:lower()
      local mine = low == me or low == "all" or low == "here" or low == "everyone"
      hls[#hls + 1] = { row, #prefix + a - 1, #prefix + b, mine and "InetMention" or "InetAt", 200 }
      s = b + 1
    end
  end
  chat_append(r, lines, hls)
end

-- ---------------------------------------------------------------- roster
local function render_roster(r)
  local rb, c = r.bufs.roster, r.client
  local names = vim.tbl_keys(c.members)
  table.sort(names, function(a, b)
    local ka, kb = c.members[a].kind, c.members[b].kind
    if ka ~= kb then return ka == "human" end
    return a:lower() < b:lower()
  end)
  local lines, hls = {}, {}
  r.roster_rows = {}
  for _, name in ipairs(names) do
    local m = c.members[name]
    local g = glyph(m.kind)
    local line = " " .. g .. " " .. name .. (name == c.name and " (you)" or "")
    hls[#hls + 1] = { #lines, 1, 1 + #g + 1 + #name, name_hl(name) }
    if m.kind == "agent" and m.state and m.state ~= "" then
      local s = #line
      line = line .. "  " .. (STATE_ICON[m.state] or "·") .. " " .. m.state
      hls[#hls + 1] = { #lines, s, #line, "InetDim" }
    end
    lines[#lines + 1] = line
    r.roster_rows[#lines] = name
    if m.kind == "agent" and m.purpose and m.purpose ~= "" then
      local p = "   " .. tostring(m.purpose):gsub("%s+", " ")
      lines[#lines + 1] = p
      r.roster_rows[#lines] = name
      hls[#hls + 1] = { #lines - 1, 0, #p, "InetSys" }
    end
  end
  vim.bo[rb].modifiable = true
  api.nvim_buf_set_lines(rb, 0, -1, false, lines)
  api.nvim_buf_clear_namespace(rb, NS, 0, -1)
  for _, h in ipairs(hls) do pcall(api.nvim_buf_set_extmark, rb, NS, h[1], h[2], { end_col = h[3], hl_group = h[4] }) end
  vim.bo[rb].modifiable = false
  refresh_bars(r)
end

-- ---------------------------------------------------------------- input line
local function update_prompt(r)
  local ib = r.bufs.input
  local text = " @" .. r.client.name .. " ❯ "
  local old = r.prompt
  if text ~= old then
    fn.prompt_setprompt(ib, text)
    local n = api.nvim_buf_line_count(ib)
    local line = api.nvim_buf_get_lines(ib, n - 1, n, false)[1]
    if old ~= "" and line:sub(1, #old) == old then
      api.nvim_buf_set_text(ib, n - 1, 0, n - 1, #old, { text })
    elseif line:sub(1, #text) ~= text then
      api.nvim_buf_set_lines(ib, n - 1, n, false, { text .. line })
    end
    r.prompt = text
  end
  api.nvim_buf_clear_namespace(ib, NS_PROMPT, 0, -1)
  local n = api.nvim_buf_line_count(ib)
  local name_end = #text - #"❯ "
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, 0, { end_col = name_end, hl_group = "InetPrompt" })
  api.nvim_buf_set_extmark(ib, NS_PROMPT, n - 1, name_end, { end_col = #text, hl_group = "InetHint" })
end

local function set_input(r, text)
  local ib = r.bufs.input
  local n = api.nvim_buf_line_count(ib)
  api.nvim_buf_set_lines(ib, n - 1, n, false, { r.prompt .. text })
  update_prompt(r)
  if valid_win(r.wins.input) then
    api.nvim_set_current_win(r.wins.input)
    api.nvim_win_set_cursor(r.wins.input, { n, #r.prompt + #text })
    vim.cmd("startinsert!")
  end
end

local function on_submit(r, text)
  vim.schedule(function()
    if rooms[r.key] == r and api.nvim_buf_is_valid(r.bufs.input) then
      api.nvim_buf_set_lines(r.bufs.input, 0, -2, false, {})
      update_prompt(r)
    end
  end)
  text = vim.trim(text or "")
  if text == "" then return end
  if text:sub(1, 1) == "/" then
    local cmd = text:match("^/(%S+)")
    if cmd == "who" then
      for name, m in pairs(r.client.members) do
        sys(r, glyph(m.kind) .. " @" .. name .. " [" .. tostring(m.state or "") .. "] " .. tostring(m.purpose or ""))
      end
    elseif cmd == "clear" then
      vim.bo[r.bufs.chat].modifiable = true
      api.nvim_buf_set_lines(r.bufs.chat, 0, -1, false, {})
      api.nvim_buf_clear_namespace(r.bufs.chat, NS, 0, -1)
      vim.bo[r.bufs.chat].modifiable = false
    elseif cmd == "quit" or cmd == "leave" then
      vim.schedule(function() R.quit(r.key) end)
    else
      sys(r, HELP)
    end
    return
  end
  if not r.client.connected then return sys(r, "not connected — :InetRoom " .. r.key .. " to reconnect", "InetErr") end
  r.client:say(text)
end

local function tab_complete(r)
  if fn.pumvisible() == 1 then return "<C-n>" end
  local col = api.nvim_win_get_cursor(0)[2]
  local partial = api.nvim_get_current_line():sub(1, col):match("@([%w_%.%-]*)$")
  if not partial then return "<Tab>" end
  local items = {}
  for name in pairs(r.client.members) do
    if name ~= r.client.name and name:lower():sub(1, #partial) == partial:lower() then items[#items + 1] = name end
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

-- ---------------------------------------------------------------- traffic
local function on_message(r, c, msg)
  if rooms[r.key] ~= r then return end
  local t = msg.t
  if t == "msg" then
    render_msg(r, msg)
    local me, mine = c.name:lower(), false
    for _, x in ipairs(msg.mentions or {}) do if x:lower() == me then mine = true end end
    for _, x in ipairs(msg.to or {}) do if x:lower() == me then mine = true end end
    if mine and msg.from ~= c.name and api.nvim_get_current_tabpage() ~= r.tab then
      vim.notify("🌐 " .. c.network .. " @" .. tostring(msg.from) .. ": " .. tostring(msg.text):sub(1, 200))
    end
  elseif t == "welcome" then
    if not r.seen_history then
      for _, h in ipairs(msg.history or {}) do render_msg(r, h) end
      r.seen_history = true
    end
    sys(r, "joined '" .. c.network .. "' (" .. c:addr() .. ") as @" .. c.name .. "   (" .. HELP .. ")")
    update_prompt(r)
    render_roster(r)
  elseif t == "members" or t == "status" then
    render_roster(r)
  elseif t == "join" or t == "leave" then
    local m = msg.member or {}
    if m.name ~= c.name then
      local purpose = (m.kind == "agent" and m.purpose and m.purpose ~= "") and (": " .. m.purpose) or ""
      sys(r, glyph(m.kind) .. " @" .. tostring(m.name) .. (t == "join" and " joined" or " left") .. purpose)
    end
  elseif t == "error" then
    sys(r, "hub: " .. tostring(msg.text), "InetErr")
  elseif t == "_info" then
    sys(r, msg.text)
  elseif t == "_error" then
    sys(r, msg.text, "InetErr")
  elseif t == "_disconnected" then
    if msg.was_connected then sys(r, "disconnected — :InetRoom " .. r.key .. " to reconnect", "InetErr") end
    render_roster(r)
  end
end

-- ---------------------------------------------------------------- layout
local function setup_view_win(win)
  local wo = vim.wo[win]
  wo.wrap, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.foldcolumn, wo.list, wo.spell, wo.cursorline = "0", false, false, false
  wo.linebreak, wo.breakindent = true, true
  wo.statusline = "%#InetHeader#"
end

local function build_layout(r)
  local w = r.wins
  vim.cmd("tabnew")
  r.tab = api.nvim_get_current_tabpage()
  local scratch = api.nvim_get_current_buf()
  w.chat = api.nvim_get_current_win()
  api.nvim_win_set_buf(w.chat, r.bufs.chat)
  if api.nvim_buf_is_valid(scratch) and scratch ~= r.bufs.chat then pcall(api.nvim_buf_delete, scratch, { force = true }) end
  setup_view_win(w.chat)
  w.roster = api.nvim_open_win(r.bufs.roster, false, { split = "right", win = w.chat, width = ROSTER_WIDTH })
  setup_view_win(w.roster)
  vim.wo[w.roster].winfixwidth = true
  w.input = api.nvim_open_win(r.bufs.input, true, { split = "below", win = -1, height = 1 })
  local wo = vim.wo[w.input]
  wo.winfixheight, wo.number, wo.relativenumber, wo.signcolumn = true, false, false, "no"
  wo.wrap, wo.cursorline, wo.foldcolumn = true, false, "0"
  wo.statusline = "%#InetHint# " .. esc(HELP) .. " │ :InetRoomQuit"
  refresh_bars(r)
  render_roster(r)
  update_prompt(r)
  api.nvim_set_current_win(w.input)
  vim.cmd("startinsert!")
end

local function layout_alive(r)
  return r.tab and api.nvim_tabpage_is_valid(r.tab) and valid_win(r.wins.chat) and valid_win(r.wins.input)
end

local function create_buffers(r)
  local bufs = {}
  for _, key in ipairs({ "chat", "roster" }) do
    local b = api.nvim_create_buf(false, true)
    pcall(api.nvim_buf_set_name, b, "inet://" .. r.key .. (key == "chat" and "" or "/" .. key))
    vim.bo[b].bufhidden, vim.bo[b].swapfile, vim.bo[b].modifiable = "hide", false, false
    vim.bo[b].filetype = "inetroom"
    bufs[key] = b
  end
  local ib = api.nvim_create_buf(false, true)
  pcall(api.nvim_buf_set_name, ib, "inet://" .. r.key .. "/input")
  vim.bo[ib].buftype, vim.bo[ib].bufhidden, vim.bo[ib].swapfile = "prompt", "hide", false
  fn.prompt_setcallback(ib, function(text) on_submit(r, text) end)
  bufs.input = ib
  return bufs
end

local function set_keymaps(r)
  local function map(modes, lhs, rhs, buf, extra)
    vim.keymap.set(modes, lhs, rhs, vim.tbl_extend("force", { nowait = true, silent = true, buffer = buf }, extra or {}))
  end
  local ib = r.bufs.input
  local quit = function() R.quit(r.key) end
  map("i", "<Tab>", function() return tab_complete(r) end, ib, { expr = true })
  map({ "n", "i" }, "<C-q>", quit, ib)
  local function scroll(keys)
    return function()
      if not valid_win(r.wins.chat) then return end
      api.nvim_win_call(r.wins.chat, function() vim.cmd("normal! " .. api.nvim_replace_termcodes(keys, true, false, true)) end)
    end
  end
  map("i", "<PageUp>", scroll("<C-b>"), ib)
  map("i", "<PageDown>", scroll("<C-f>"), ib)
  map("i", "<S-Up>", scroll("<C-y>"), ib)
  map("i", "<S-Down>", scroll("<C-e>"), ib)
  local function to_input() set_input(r, api.nvim_buf_get_lines(ib, -2, -1, false)[1]:sub(#r.prompt + 1)) end
  for _, b in ipairs({ r.bufs.chat, r.bufs.roster }) do
    for _, lhs in ipairs({ "i", "a", "I", "A", "o" }) do map("n", lhs, to_input, b) end
    map("n", "<C-q>", quit, b)
  end
  map("n", "<CR>", to_input, r.bufs.chat)
  map("n", "<CR>", function()
    local name = r.roster_rows and r.roster_rows[api.nvim_win_get_cursor(0)[1]]
    if name and name ~= r.client.name then set_input(r, "@" .. name .. " ") else to_input() end
  end, r.bufs.roster)
  map("n", "G", function()
    r.follow = true
    api.nvim_win_set_cursor(0, { api.nvim_buf_line_count(0), 0 })
    refresh_bars(r)
  end, r.bufs.chat)
end

local group = api.nvim_create_augroup("InetRoom", { clear = true })
api.nvim_create_autocmd("WinScrolled", {
  group = group,
  callback = function()
    for _, r in pairs(rooms) do
      if valid_win(r.wins.chat) then
        local f = api.nvim_win_call(r.wins.chat, function() return fn.line("w$") >= fn.line("$") end)
        if f ~= r.follow then
          r.follow = f
          refresh_bars(r)
        end
      end
    end
  end,
})
api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function() for _, r in pairs(rooms) do r.client:send({ t = "bye" }) end end,
})

-- ---------------------------------------------------------------- public API
--- opts: network (name | "host:port" | nil = default), name (your name; default g:inet_user)
function R.open(opts)
  opts = opts or {}
  define_highlights()
  local net, err = I.network(opts.network)
  if not net then return I.notify(err, vim.log.levels.WARN) end
  local r = rooms[net.name]
  if r then
    if not layout_alive(r) then build_layout(r) else api.nvim_set_current_tabpage(r.tab) end
    if not r.client.tcp then r.client:connect() end
    return
  end
  local cfg = I.config()
  r = { key = net.name, wins = {}, follow = true, prompt = "" }
  r.client = I.client({ network = net, name = opts.name or cfg.user, kind = "human", purpose = "human",
    watch = cfg.watch, on_message = function(c, msg) on_message(r, c, msg) end })
  rooms[r.key] = r
  r.bufs = create_buffers(r)
  set_keymaps(r)
  build_layout(r)
  r.client:connect()
end

--- Leave a room: by network name, or the room of the current tab.
function R.quit(key)
  local r = key and rooms[key]
  if not r then
    local tab = api.nvim_get_current_tabpage()
    for _, x in pairs(rooms) do if x.tab == tab then r = x end end
  end
  if not r then return I.notify("no room " .. (key and ("'" .. key .. "' ") or "in this tab")) end
  rooms[r.key] = nil
  r.client:close()
  if r.tab and api.nvim_tabpage_is_valid(r.tab) and #api.nvim_list_tabpages() > 1 then
    pcall(vim.cmd, "tabclose " .. api.nvim_tabpage_get_number(r.tab))
  end
  for _, b in pairs(r.bufs) do
    if api.nvim_buf_is_valid(b) then pcall(api.nvim_buf_delete, b, { force = true }) end
  end
end

function R.list()
  local out = {}
  for key, r in pairs(rooms) do
    out[#out + 1] = key .. " (" .. r.client:addr() .. ") as @" .. r.client.name
      .. (r.client.connected and "" or " [offline]")
  end
  return out
end

package.loaded.inet_room = R
return R
