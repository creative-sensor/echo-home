-- local uid =  os.getenv("UID") or "1000" 
-- local cpf = "/run/user/" .. uid .. "/cpwlvim"

local M = {}

function M.write(cpf)
  local file = io.open(cpf, "w")
  local data = vim.fn.getreg('"')
  local encoded = vim.fn.system('base64', data)
  file:write(encoded)
  file:close()
end

return M

