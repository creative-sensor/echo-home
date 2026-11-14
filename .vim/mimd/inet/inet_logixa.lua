-- inet_logixa.lua: conversation logging for Intelligence Network rooms.
--
-- Logs the full chat buffer to ~/echo-home/Documents/logixa/ in real time.
-- Default file: <YYYY-MM-DD> (today's date). With a theme: <theme-name> instead.
-- On room open, previous content is loaded back into the chat buffer.
-- L#<n> in a message quotes line n from the log.

do
  local prev = package.loaded.inet_logixa
  if type(prev) == "table" and prev.new then return prev end
end

local M = {}
M.__index = M

local fn = vim.fn
local BASE = fn.expand("~/echo-home/Documents/logixa")

local function ensure_dir(dir)
  if fn.isdirectory(dir) == 0 then fn.mkdir(dir, "p") end
end

local function sanitize(name)
  return tostring(name):gsub("[^%w%-%._]", "-"):gsub("%-+", "-"):sub(1, 100)
end

--- Create a new logixa logger (one per room).
function M.new()
  return setmetatable({ theme = nil, lines = {}, _fh = nil, _path = nil }, M)
end

--- Resolve the current log file path.
function M:path()
  return BASE .. "/" .. (self.theme or os.date("%Y-%m-%d"))
end

--- Open (or reopen) the log file; loads existing content into self.lines.
function M:open()
  local p = self:path()
  if self._path == p and self._fh then return self.lines end
  self:close()
  ensure_dir(BASE)
  self._path = p
  self.lines = {}
  if fn.filereadable(p) == 1 then
    for line in io.lines(p) do self.lines[#self.lines + 1] = line end
  end
  self._fh = io.open(p, "a")
  return self.lines
end

--- Append rendered lines to the log (called every time the chat buffer grows).
function M:append(new_lines)
  if not self._fh then return end
  for _, line in ipairs(new_lines) do
    self.lines[#self.lines + 1] = line
    self._fh:write(line .. "\n")
  end
  self._fh:flush()
end

--- Return the content of log line n (1-based).
function M:line(n)
  return self.lines[tonumber(n) or 0]
end

--- Total logged lines.
function M:count()
  return #self.lines
end

--- Switch theme (nil/""/off/reset/date clears the theme back to date-based). Reopens the file.
function M:set_theme(name)
  local t = (name and name ~= "" and name ~= "off" and name ~= "reset" and name ~= "date")
    and sanitize(name) or nil
  if t == self.theme then return self.lines end
  self.theme = t
  return self:open()
end

--- Close the file handle.
function M:close()
  if self._fh then self._fh:close(); self._fh = nil end
  self._path = nil
end

package.loaded.inet_logixa = M
return M
