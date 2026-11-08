-- inet.lua: Intelligence Network driver for Neovim (requires Neovim >= 0.10).
--
-- The common base every agent frontend and the human room build on:
--   * networks   named networks from vimpage_inet (g:inet_network, g:inet_networks)
--   * hub        I.up(net) brings a network up (starts inet_hub.py when nothing listens locally),
--                I.down(net) stops one this Neovim started
--   * client     I.client{...}: a Lua connection (hello/say/status/profile/members) — the room uses it;
--                a Lua-side agent can use it too
--   * agents     an agent frontend registers an adapter, I.register_agent(key, {join, leave, status});
--                I.join(key, overrides) resolves its profile (g:inet_agents[key] + overrides),
--                brings the network up and hands the profile to the adapter.
--                Python agent backends connect with inet_client.py using I.agent_args(profile).
--
-- Commands (all take key=value overrides; purpose= swallows the rest of the line)
--   :InetUp [network]                     :InetDown [network]
--   :InetRoom [network] [name]            :InetRoomQuit
--   :InetJoin <agent> [name] [purpose…]   :InetLeave <agent>         :InetStatus
--   e.g. :InetJoin memphia scout net=lab purpose=watches the CI logs
--
-- Settings (see mimd/vimpage_inet)
--   g:inet_network   {name, host, port}   the default network        (default inet @ 127.0.0.1:7788)
--   g:inet_networks  {name: {host, port}} more named networks
--   g:inet_user      your name in rooms   (default $USER)
--   g:inet_watch     1: humans also see agents' point-to-point traffic (dimmed)
--   g:inet_agents    {agent: {name, purpose, network}} join profiles
--   g:inet_python    python for the hub  (falls back to g:memphia_python, python3)
--   g:inet_hub_detach  1: hubs outlive this Neovim

do
  local prev = package.loaded.inet
  if type(prev) == "table" and prev._driver then return prev end
end

local I = { _driver = true }
local api, fn = vim.api, vim.fn
local uv = vim.uv or vim.loop

local function script_dir()
  local d = fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
  if fn.has("win32") == 1 then
    local drive, rest = d:match("^[/\\]([a-zA-Z])[/\\](.*)$")
    if drive and fn.isdirectory(d) == 0 then d = drive:upper() .. ":/" .. rest end
    d = d:gsub("\\", "/")
  end
  return d
end
I.dir = script_dir()

local hubs = {}     -- "host:port" -> {job, net, log}: hubs started by this Neovim
local adapters = {} -- agent key -> {join = fn(profile), leave = fn(), status = fn() -> table|nil}

function I.notify(msg, level) vim.notify("inet: " .. msg, level or vim.log.levels.INFO) end

-- ---------------------------------------------------------------- settings & networks
function I.config()
  local g = vim.g
  local watch = g.inet_watch
  return {
    python = g.inet_python or g.memphia_python
      or (fn.has("win32") == 1 and fn.executable("python") == 1 and "python" or "python3"),
    user = g.inet_user or vim.env.USER or vim.env.USERNAME or "human",
    watch = watch == nil or (watch ~= false and watch ~= 0),
    detach = g.inet_hub_detach == true or g.inet_hub_detach == 1,
  }
end

local function norm(name, spec)
  return { name = spec.name or name or "inet", host = spec.host or "127.0.0.1", port = tonumber(spec.port) or 7788 }
end

--- Resolve a network: nil/"" = default, a name from g:inet_network/g:inet_networks, "host:port", or a table.
function I.network(which)
  if type(which) == "table" then return norm(nil, which) end
  local default = vim.g.inet_network or {}
  if type(default) == "string" then default = { name = default } end
  if which == nil or which == "" or which == (default.name or "inet") then return norm(nil, default) end
  local nets = vim.g.inet_networks or {}
  if type(nets[which]) == "table" then return norm(which, nets[which]) end
  local host, port = tostring(which):match("^(.*):(%d+)$")
  if port then
    host = host ~= "" and host or "127.0.0.1"
    return { name = host .. ":" .. port, host = host, port = tonumber(port) }
  end
  return nil, "unknown network '" .. tostring(which) .. "' (define it in g:inet_network / g:inet_networks)"
end

function I.networks()
  local out = { I.network() }
  for name, spec in pairs(vim.g.inet_networks or {}) do
    if name ~= out[1].name then out[#out + 1] = norm(name, spec) end
  end
  return out
end

function I.addr(net) return net.host .. ":" .. net.port end

local function is_local(host) return host == "127.0.0.1" or host == "localhost" or host == "::1" or host == "0.0.0.0" end

function I.resolve_host(host)
  if host == "localhost" then return "127.0.0.1" end
  if host:match("^%d+%.%d+%.%d+%.%d+$") then return host end
  local res = uv.getaddrinfo(host, nil, { family = "inet", socktype = "stream" })
  return res and res[1] and res[1].addr or host
end

-- ---------------------------------------------------------------- hub lifecycle
local function probe(net, cb)
  local tcp = uv.new_tcp()
  local ok = pcall(tcp.connect, tcp, I.resolve_host(net.host), net.port, function(err)
    tcp:close()
    vim.schedule(function() cb(err == nil) end)
  end)
  if not ok then
    tcp:close()
    vim.schedule(function() cb(false) end)
  end
end

--- Make sure the network is up; cb(ok, net_or_error). info(text) gets progress messages.
function I.up(which, cb, info)
  cb = cb or function() end
  info = info or function(t) I.notify(t) end
  local net, err = I.network(which)
  if not net then
    I.notify(err, vim.log.levels.WARN)
    return cb(false, err)
  end
  local key = I.addr(net)
  probe(net, function(up)
    if up then return cb(true, net) end
    if not is_local(net.host) then return cb(false, "network '" .. net.name .. "' is not reachable at " .. key) end
    if not hubs[key] then
      local cfg = I.config()
      local h = { net = net, log = {} }
      h.job = fn.jobstart({ cfg.python, "-u", I.dir .. "/inet_hub.py", "--name", net.name,
        "--host", I.resolve_host(net.host), "--port", tostring(net.port) }, {
        detach = cfg.detach,
        on_stderr = function(_, data)
          for _, l in ipairs(data) do
            if l ~= "" then
              h.log[#h.log + 1] = l
              if #h.log > 50 then table.remove(h.log, 1) end
            end
          end
        end,
        on_exit = function(_, code)
          if hubs[key] == h then hubs[key] = nil end
          if code ~= 0 and vim.v.exiting == vim.NIL then
            vim.schedule(function()
              I.notify("network '" .. net.name .. "' stopped (exit " .. code .. "): " .. (h.log[#h.log] or ""),
                vim.log.levels.WARN)
            end)
          end
        end,
      })
      if h.job <= 0 then return cb(false, "failed to start " .. cfg.python .. " inet_hub.py") end
      hubs[key] = h
      info("starting network '" .. net.name .. "' at " .. key)
    end
    local tries = 0
    local function again()
      probe(net, function(ok)
        if ok then
          info("network '" .. net.name .. "' is up at " .. key)
          return cb(true, net)
        end
        tries = tries + 1
        if tries > 25 or not hubs[key] then return cb(false, "network '" .. net.name .. "' did not come up at " .. key) end
        vim.defer_fn(again, 200)
      end)
    end
    again()
  end)
end

function I.down(which)
  local net, err = I.network(which)
  if not net then return I.notify(err, vim.log.levels.WARN) end
  local h = hubs[I.addr(net)]
  if not h then return I.notify("network '" .. net.name .. "' was not started by this Neovim") end
  hubs[I.addr(net)] = nil
  fn.jobstop(h.job)
  I.notify("network '" .. net.name .. "' is down")
end

-- ---------------------------------------------------------------- client
-- opts: network, name, kind ("human"|"agent"), purpose, watch, autostart (default true),
--       on_message(client, msg)  hub messages plus local events:
--         {t="_info",text} {t="_error",text} {t="_disconnected",reason,was_connected}
local Client = {}
Client.__index = Client

function I.client(opts)
  local net, err = I.network(opts.network)
  if not net then return nil, err end
  return setmetatable({ o = opts, net = net, network = net.name, name = opts.name or I.config().user,
    members = {}, connected = false, partial = "" }, Client)
end

function Client:_emit(msg) if self.o.on_message then self.o.on_message(self, msg) end end
function Client:addr() return I.addr(self.net) end
function Client:is_open() return type(self.tcp) == "userdata" and not self.tcp:is_closing() end

function Client:_handle(msg)
  local t = msg.t
  if t == "welcome" then
    self.name, self.network, self.connected = msg.name or self.name, msg.network or self.network, true
    self.members = {}
    for _, m in ipairs(msg.members or {}) do self.members[m.name] = m end
  elseif t == "members" then
    self.members = {}
    for _, m in ipairs(msg.members or {}) do self.members[m.name] = m end
  elseif t == "status" and self.members[msg.name] then
    self.members[msg.name].state = msg.state
  end
  self:_emit(msg)
end

function Client:_lost(reason)
  if self:is_open() then self.tcp:close() end
  self.tcp = nil
  local was = self.connected
  self.connected, self.members = false, {}
  self:_emit({ t = "_disconnected", reason = reason, was_connected = was })
end

function Client:_open()
  local tcp = uv.new_tcp()
  self.tcp, self.partial = tcp, ""
  local function fail(err)
    vim.schedule(function()
      if self.tcp ~= tcp then return end
      self.tcp = nil
      self:_emit({ t = "_error", text = "cannot reach '" .. self.net.name .. "' at " .. self:addr() .. " (" .. tostring(err) .. ")" })
    end)
  end
  local ok = pcall(tcp.connect, tcp, I.resolve_host(self.net.host), self.net.port, function(err)
    if err then
      tcp:close()
      return fail(err)
    end
    tcp:read_start(function(rerr, chunk)
      if rerr or not chunk then
        vim.schedule(function() if self.tcp == tcp then self:_lost(rerr) end end)
        return
      end
      vim.schedule(function()
        if self.tcp ~= tcp then return end
        local lines = vim.split(self.partial .. chunk, "\n", { plain = true })
        self.partial = table.remove(lines)
        for _, line in ipairs(lines) do
          if line ~= "" then
            local okj, msg = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
            if okj and type(msg) == "table" then self:_handle(msg) end
          end
        end
      end)
    end)
    vim.schedule(function()
      if self.tcp == tcp then
        self:send({ t = "hello", name = self.name, kind = self.o.kind or "human", purpose = self.o.purpose or "",
          watch = self.o.watch and true or false })
      end
    end)
  end)
  if not ok then
    tcp:close()
    fail("bad address")
  end
end

--- Connect (bringing the network up first unless autostart = false).
function Client:connect()
  if self.tcp or self.connecting then return end
  self.closed = false
  if self.o.autostart == false then return self:_open() end
  self.connecting = true
  I.up(self.net, function(ok, err)
    self.connecting = false
    if self.closed then return end
    if ok then self:_open() else self:_emit({ t = "_error", text = err }) end
  end, function(text) self:_emit({ t = "_info", text = text }) end)
end

function Client:send(msg)
  if not self:is_open() then return false end
  self.tcp:write(vim.json.encode(msg) .. "\n")
  return true
end

--- say(text [, {to = {names}, re = id}]): without `to` the hub routes by @mentions.
function Client:say(text, opts)
  opts = opts or {}
  local msg = { t = "say", text = text, re = opts.re }
  if opts.to and #opts.to > 0 then msg.to = opts.to end
  return self:send(msg)
end

function Client:status(state) return self:send({ t = "status", state = state }) end
function Client:profile(purpose) return self:send({ t = "profile", purpose = purpose }) end

function Client:close()
  self.closed = true
  self:send({ t = "bye" })
  local tcp = self.tcp
  self.tcp, self.connected = nil, false
  if type(tcp) == "userdata" then
    vim.defer_fn(function() if not tcp:is_closing() then tcp:close() end end, 100)
  end
end

-- ---------------------------------------------------------------- agents
function I.register_agent(key, adapter) adapters[key] = adapter end

function I.agents()
  local seen, out = {}, {}
  for k in pairs(adapters) do seen[k] = true end
  for k in pairs(vim.g.inet_agents or {}) do seen[k] = true end
  for k in pairs(seen) do out[#out + 1] = k end
  table.sort(out)
  return out
end

--- Join profile of an agent: g:inet_agents[key] overlaid with overrides {name, purpose, network}.
function I.profile(key, over)
  over = over or {}
  local prof = (vim.g.inet_agents or {})[key] or {}
  local net, err = I.network(over.network or prof.network)
  if not net then return nil, err end
  return { key = key, name = over.name or prof.name or key, purpose = over.purpose or prof.purpose or "",
    network = net, hub = I.addr(net) }
end

--- CLI args for a Python agent backend built on inet_client.py.
function I.agent_args(p)
  local args = { "--net-network", p.network.name, "--net-hub", p.hub, "--net-name", p.name }
  if p.purpose ~= "" then vim.list_extend(args, { "--net-purpose", p.purpose }) end
  return args
end

function I.join(key, over)
  local a = adapters[key]
  if not a then
    return I.notify("no agent '" .. tostring(key) .. "' is loaded (source its frontend first, e.g. mmph1)",
      vim.log.levels.WARN)
  end
  local p, err = I.profile(key, over)
  if not p then return I.notify(err, vim.log.levels.WARN) end
  I.up(p.network, function(ok, e)
    if not ok then return I.notify(e, vim.log.levels.WARN) end
    a.join(p)
    I.notify("@" .. p.name .. " (" .. key .. ") joining '" .. p.network.name .. "'")
  end)
end

function I.leave(key)
  local a = adapters[key]
  if not a then return I.notify("no agent '" .. tostring(key) .. "' is loaded", vim.log.levels.WARN) end
  a.leave()
end

function I.status()
  local lines = {}
  for _, net in ipairs(I.networks()) do
    local h = hubs[I.addr(net)]
    lines[#lines + 1] = "network " .. net.name .. " @ " .. I.addr(net) .. (h and "  [hub started here]" or "")
  end
  for _, key in ipairs(I.agents()) do
    local a = adapters[key]
    local st = a and a.status and a.status()
    local p = I.profile(key) or { name = key, network = { name = "?" } }
    local s
    if not a then
      s = "not loaded"
    elseif st and st.joined then
      s = (st.connected and "on " or "connecting to ") .. tostring(st.network or p.network.name)
        .. " as @" .. tostring(st.name) .. " [" .. tostring(st.state) .. "]"
    else
      s = "loaded, not joined (profile: @" .. p.name .. " → " .. p.network.name .. ")"
    end
    lines[#lines + 1] = "agent " .. key .. ": " .. s
  end
  local R = package.loaded.inet_room
  if R and R.list then
    for _, r in ipairs(R.list()) do lines[#lines + 1] = "room " .. r end
  end
  I.notify(table.concat(lines, "\n"))
end

-- ---------------------------------------------------------------- room (human UI)
local function room_module()
  return package.loaded.inet_room or dofile(I.dir .. "/inet_room.lua")
end
function I.room(opts) room_module().open(opts or {}) end
function I.room_quit(net) if package.loaded.inet_room then package.loaded.inet_room.quit(net) end end

-- ---------------------------------------------------------------- commands
--- "a b k=v purpose=rest of line" -> {positional..., k = v, purpose = "rest of line"}
local function parse(line, positional, rest_key)
  local out = {}
  local purpose = line:match("purpose=(.*)$")
  if purpose then
    out.purpose = vim.trim(purpose)
    line = line:gsub("purpose=.*$", "")
  end
  local pos = {}
  for tok in line:gmatch("%S+") do
    local k, v = tok:match("^(%w+)=(.*)$")
    if k then out[k == "net" and "network" or k] = v else pos[#pos + 1] = tok end
  end
  for i, name in ipairs(positional) do
    if pos[i] and out[name] == nil then out[name] = pos[i] end
  end
  if rest_key and #pos > #positional and out[rest_key] == nil then
    out[rest_key] = table.concat(pos, " ", #positional + 1)
  end
  return out
end

local function complete_networks()
  local out = {}
  for _, n in ipairs(I.networks()) do out[#out + 1] = n.name end
  return out
end

local cmd = api.nvim_create_user_command
cmd("InetUp", function(o) I.up(parse(o.args, { "network" }).network) end,
  { nargs = "?", complete = complete_networks, desc = "Bring an Intelligence Network up" })
cmd("InetDown", function(o) I.down(parse(o.args, { "network" }).network) end,
  { nargs = "?", complete = complete_networks, desc = "Stop a network started by this Neovim" })
cmd("InetRoom", function(o) I.room(parse(o.args, { "network", "name" })) end,
  { nargs = "*", complete = complete_networks, desc = "Open a network room: [network] [name]" })
cmd("InetRoomQuit", function(o) I.room_quit(o.args ~= "" and o.args or nil) end,
  { nargs = "?", complete = complete_networks, desc = "Leave a network room" })
cmd("InetJoin", function(o)
  local a = parse(o.args, { "agent", "name" }, "purpose")
  if not a.agent then return I.notify("usage: :InetJoin <agent> [name] [purpose…]  (agents: "
      .. table.concat(I.agents(), ", ") .. ")", vim.log.levels.WARN) end
  I.join(a.agent, a)
end, { nargs = "+", complete = function() return I.agents() end, desc = "Plug an agent into a network" })
cmd("InetLeave", function(o) I.leave(vim.trim(o.args)) end,
  { nargs = 1, complete = function() return I.agents() end, desc = "Unplug an agent from its network" })
cmd("InetStatus", I.status, { desc = "Networks, agents and rooms" })

package.loaded.inet = I
return I
