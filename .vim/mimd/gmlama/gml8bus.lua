-- gml8bus.lua: the discovery bus of the LLM services gml8 provides (requires Neovim >= 0.10).
-- Loaded by ~/echo-home/.vim/mimd/gml8 (the vimpage) and by gml8.lua; any agent shell can use it:
--
--   local bus = package.loaded.gml8bus or dofile(vim.fn.expand("~/echo-home/.vim/mimd/gmlama/gml8bus.lua"))
--   bus.services()                      every service gml8 knows: {key, name, model, port, base_url, state, kind,
--                                       vision, n_ctx, power, origin, …} (state "up" = answers on /v1)
--   bus.discover{vision=true}           the best service that is up and matches (kind "chat" by default; min_ctx,
--                                       name, model, port) or nil
--   bus.port(want, default)             its port, else default (g:LMSH_PORT, else 8080)
--   bus.resolve_args(args, want)        an agent's args with "--port gml8" (or "--port auto") made a real port
--   bus.ensure(want, cb)                cb(service) once one is up — asks gml8 to launch a matching entry when none
--                                       runs (cb(nil, err) when gml8 is not running or nothing matches)
--   bus.subscribe(fn) -> id             fn(list, changes) on every change; bus.unsubscribe(id)
--   autocmds: User Gml8Services (data = {services = list}), User Gml8Ready / User Gml8Gone (data = the service)
--   VimL: Gml8Port([want [, default]]), :Gml8Services
--
-- While gml8 runs in this Neovim it publishes here as the services change. Without it (gml8q: the services keep
-- running), the bus reads gml8's registry ~/echo-home/memory/gml8/<hostname>/run/services.json and, for discover /
-- port, checks that the service answers before it hands it out.

local M = package.loaded.gml8bus or {}
local api, fn = vim.api, vim.fn

M.live = M.live or nil -- the list gml8 published (nil: gml8 is not running here)
M.subs = M.subs or {}
M.seq = M.seq or 0
M.waiting = M.waiting or {} -- rid -> {cb, want}
M.provider = M.provider or nil -- {request = function(want, rid)} while gml8 runs

local function hostname()
  return vim.g.hostname or vim.trim(fn.system("hostname"))
end

local function registry_path()
  local base = vim.g.gml8_memory and fn.expand(vim.g.gml8_memory) or fn.expand("~/echo-home/memory/gml8")
  return base .. "/" .. hostname() .. "/run/services.json"
end

--- The registry gml8 writes (what ran when it last looked).
function M.read_registry()
  local path = registry_path()
  if fn.filereadable(path) == 0 then return {} end
  local ok, data = pcall(vim.json.decode, table.concat(fn.readfile(path), "\n"))
  if not ok or type(data) ~= "table" then return {} end
  return type(data.services) == "table" and data.services or {}
end

function M.services()
  if M.live then return M.live end
  return M.read_registry()
end

--- Does the service answer on /v1/models now? (only when gml8 is not here to say so)
local function answers(port)
  local out = fn.system({ "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "1.5",
    "http://127.0.0.1:" .. tostring(port) .. "/v1/models" })
  return vim.trim(out) == "200"
end

local function matches(s, want)
  want = want or {}
  if s.state ~= "up" then return false end
  if want.port and tonumber(s.port) ~= tonumber(want.port) then return false end
  if (s.kind or "chat") ~= (want.kind or "chat") then return false end
  if want.vision and not s.vision then return false end
  if want.min_ctx and (tonumber(s.n_ctx) or 0) < want.min_ctx then return false end
  local hay = (tostring(s.key) .. " " .. tostring(s.name) .. " " .. tostring(s.profile)):lower()
  if want.name and not hay:find(want.name:lower(), 1, true) then return false end
  if want.model and not tostring(s.model):lower():find(want.model:lower(), 1, true) then return false end
  return true
end

--- The services that match, best first: the one powering gml8, then the largest context, then the oldest.
function M.rank(want, list)
  local out = {}
  for _, s in ipairs(list or M.services()) do
    if matches(s, want) then out[#out + 1] = s end
  end
  table.sort(out, function(a, b)
    if (a.power and 1 or 0) ~= (b.power and 1 or 0) then return a.power == true end
    if (tonumber(a.n_ctx) or 0) ~= (tonumber(b.n_ctx) or 0) then return (tonumber(a.n_ctx) or 0) > (tonumber(b.n_ctx) or 0) end
    return tostring(a.started or "") < tostring(b.started or "")
  end)
  return out
end

function M.discover(want)
  for _, s in ipairs(M.rank(want)) do
    if M.live or answers(s.port) then return s end
  end
  return nil
end

function M.port(want, default)
  local s = M.discover(type(want) == "table" and want or {})
  if s then return tonumber(s.port) end
  return tonumber(default) or tonumber(vim.g.LMSH_PORT) or 8080
end

--- "--port gml8" / "--port auto" / "--port gml8:vision" in an agent's argument list -> the port of a service.
function M.resolve_args(args, want)
  local out = {}
  local i = 1
  while i <= #args do
    local a = tostring(args[i])
    local nxt = args[i + 1] and tostring(args[i + 1]) or nil
    if a == "--port" and nxt and (nxt == "auto" or nxt:match("^gml8")) then
      local w = vim.deepcopy(want or {})
      local sel = nxt:match("^gml8:(.+)$")
      if sel == "vision" then w.vision = true elseif sel then w.name = sel end
      local port = M.port(w, vim.g.LMSH_PORT or 8080)
      out[#out + 1], out[#out + 2] = "--port", tostring(port)
      i = i + 2
    else
      out[#out + 1] = a
      i = i + 1
    end
  end
  return out
end

function M.subscribe(f)
  M.seq = M.seq + 1
  M.subs[M.seq] = f
  return M.seq
end

function M.unsubscribe(id) M.subs[id] = nil end

local function fire(pattern, data)
  pcall(api.nvim_exec_autocmds, "User", { pattern = pattern, data = data, modeline = false })
end

--- gml8 publishes the services (on every change).
function M.publish(list)
  local before = {}
  for _, s in ipairs(M.live or {}) do before[tostring(s.key) .. ":" .. tostring(s.port)] = s end
  local now, changes = {}, { ready = {}, gone = {} }
  for _, s in ipairs(list or {}) do
    local id = tostring(s.key) .. ":" .. tostring(s.port)
    now[id] = s
    local b = before[id]
    if s.state == "up" and not (b and b.state == "up") then changes.ready[#changes.ready + 1] = s end
  end
  for id, s in pairs(before) do
    if s.state == "up" and not (now[id] and now[id].state == "up") then changes.gone[#changes.gone + 1] = s end
  end
  M.live = list or {}
  vim.g.gml8_services = M.live
  for _, s in ipairs(changes.ready) do fire("Gml8Ready", s) end
  for _, s in ipairs(changes.gone) do fire("Gml8Gone", s) end
  fire("Gml8Services", { services = M.live })
  for _, f in pairs(M.subs) do pcall(f, M.live, changes) end
end

--- gml8 plugs in (request: ask it for a service) / out (quit: the services keep running; the registry speaks for them).
function M.attach(provider) M.provider = provider end

function M.detach()
  M.provider = nil
  M.live = nil
  vim.g.gml8_services = nil
  for rid, w in pairs(M.waiting) do
    M.waiting[rid] = nil
    pcall(w.cb, nil, "gml8 stopped")
  end
end

--- An agent needs a service: now when one is up, else gml8 launches a matching entry and cb runs once it answers.
function M.ensure(want, cb)
  want = want or {}
  local s = M.discover(want)
  if s then return cb(s) end
  if not M.provider then return cb(nil, "gml8 is not running (gml8e starts it)") end
  M.seq = M.seq + 1
  local rid = "r" .. M.seq
  M.waiting[rid] = { cb = cb, want = want }
  M.provider.request(want, rid)
end

--- gml8's answer to ensure(): the service, or a launch under way (wait), or an error.
function M._provided(msg)
  local w = M.waiting[msg.rid]
  if not w then return end
  if type(msg.service) == "table" then
    M.waiting[msg.rid] = nil
    pcall(w.cb, msg.service)
  elseif msg.error and msg.error ~= vim.NIL then
    M.waiting[msg.rid] = nil
    pcall(w.cb, nil, msg.error)
  end -- launching: the next answer comes when it is up
end

function M.describe(s)
  return string.format("%s :%s %s %s%s%s%s", tostring(s.key), tostring(s.port), tostring(s.state), tostring(s.model),
    s.n_ctx and s.n_ctx ~= vim.NIL and (" · n_ctx " .. tostring(s.n_ctx)) or "", s.vision and " · vision" or "",
    s.power and " · ⚡ gml8" or "")
end

if not M._defined then
  M._defined = true
  vim.cmd([[
function! Gml8Port(...) abort
  return luaeval('package.loaded.gml8bus.port(_A[1], _A[2])', [a:0 > 0 ? a:1 : {}, a:0 > 1 ? a:2 : get(g:, 'LMSH_PORT', 8080)])
endfunction
]])
  api.nvim_create_user_command("Gml8Services", function()
    local list = M.services()
    if #list == 0 then return vim.notify("gml8: no LLM service known (gml8e starts gml8)") end
    local lines = {}
    for _, s in ipairs(list) do lines[#lines + 1] = M.describe(s) .. "  " .. tostring(s.base_url) end
    vim.notify(table.concat(lines, "\n") .. (M.live and "" or "\n(from gml8's registry: gml8 is not running here)"))
  end, { desc = "The LLM services gml8 provides" })
end

package.loaded.gml8bus = M
return M
