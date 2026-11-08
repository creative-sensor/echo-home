-- neos_doc.lua: build the Neos agent document of the user's own Vim setup.
--
-- Reads ~/.vimrc.neo plus every mini module it sources (exec 'source '.g:MIMD_BASE."/x" or
-- `source path`) and turns keymaps, commands, functions, autocmds and settings into a compact
-- markdown reference: one entry per line, grouped by the "-------- [SECTION] --------" headers.
-- Comment lines describe entries the same way the rc file does:
--   indented  "comment  right after an entry  -> note on that entry
--   column-0  "comment  right before an entry -> note on the next entry
-- Commented-out code is skipped. Extra markdown files (default: the :AgentCapabilities handbook)
-- are appended verbatim.
--
-- neos.py searches this file to remind the user how their own functions and keys work.
--   :NeosDoc        regenerate now (neos.lua also regenerates it on start when the rc is newer)
-- Options: g:neos_vimrc (default ~/.vimrc.neo), g:neos_doc_file, g:neos_doc_extra (list of paths).

local fn = vim.fn
local D = {}

local SCRIPT_DIR = fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
D.default_file = SCRIPT_DIR .. "/agent_doc.md"

local MAP_PREFIX = {
  [""] = "map", n = "nmap", v = "vmap", x = "xmap", s = "smap", o = "omap", i = "imap", l = "lmap", c = "cmap", t = "tmap",
  nore = "noremap", nnore = "nnoremap", vnore = "vnoremap", xnore = "xnoremap", snore = "snoremap",
  onore = "onoremap", inore = "inoremap", lnore = "lnoremap", cnore = "cnoremap", tnore = "tnoremap",
}
local CODE_WORDS = {}
for w in ([[set setlocal map nmap vmap xmap imap tmap noremap nnoremap vnoremap xnoremap inoremap tnoremap
  autocmd au let unlet highlight hi call exec execute colorscheme source lua if else elseif endif for endfor
  function endfunction command syntax filetype normal Plugin Bundle echo return while endwhile try endtry
  silent redir]]):gmatch("%S+") do CODE_WORDS[w] = true end

local function expand(p) return fn.expand(p) end
local function short(p) return fn.fnamemodify(p, ":~") end

local function looks_like_code(c)
  local w = c:match("^([%a_]+)!?%s") or c:match("^([%a_]+)!?$")
  return (w and CODE_WORDS[w]) or c:match("^%w+%s*=") ~= nil
end

local function clean_comment(line)
  local c = line:gsub('^%s*"%s?', "")
  c = c:gsub("^:", "")
  return vim.trim(c)
end

local function strip_map_opts(rest)
  local again = true
  while again do
    again = false
    for _, o in ipairs({ "<buffer>", "<silent>", "<nowait>", "<expr>", "<unique>", "<script>", "<special>" }) do
      if rest:sub(1, #o):lower() == o then
        rest = vim.trim(rest:sub(#o + 1))
        again = true
      end
    end
  end
  return rest
end

local function parse_file(path)
  local ok, lines = pcall(fn.readfile, path)
  if not ok then return nil end
  local sections, order = {}, {}
  local section, last, pending, cur_fn = "(top)", nil, {}, nil

  local function sec()
    if not sections[section] then
      sections[section] = { entries = {}, settings = {}, vars = {} }
      order[#order + 1] = section
    end
    return sections[section]
  end
  local function flush_notes()
    if #pending > 0 then
      table.insert(sec().entries, { kind = "note", name = nil, detail = "", notes = pending })
      pending = {}
    end
  end
  local function add(kind, name, detail)
    local notes = {}
    for _, c in ipairs(pending) do notes[#notes + 1] = c end
    pending = {}
    local e = { kind = kind, name = name, detail = detail, notes = notes }
    table.insert(sec().entries, e)
    last = e
    return e
  end

  for _, raw in ipairs(lines) do
    local line = raw:gsub("\r$", "")
    local t = vim.trim(line)
    local sname, sub = line:match('^"%-%-%-+%s*%[(.-)%]%s*%-*%s*(.*)$')
    if sname then
      flush_notes()
      sub = vim.trim(sub or "")
      section = sub ~= "" and (sname .. " · " .. sub) or sname
      last, pending = nil, {}
    elseif cur_fn then
      if t:match("^endf") then
        cur_fn = nil
      elseif t:match('^"') then
        local c = clean_comment(t)
        if c ~= "" and not looks_like_code(c) then table.insert(cur_fn.notes, c) end
      elseif t ~= "" and #cur_fn.body < 4 then
        table.insert(cur_fn.body, t)
      end
    elseif t:match('^"') then
      local c = clean_comment(t)
      if c ~= "" and not looks_like_code(c) and not c:match("^%-%-%-") then
        if line:match('^%s+"') and last then table.insert(last.notes, c) else pending[#pending + 1] = c end
      elseif c ~= "" then
        last = nil -- commented-out code: its indented notes describe that code, not the entry above
      end
    elseif t == "" then
      flush_notes()
      last = nil
    else
      local prefix, bang, rest = t:match("^(%a*)map(!?)%s+(.+)$")
      if prefix and MAP_PREFIX[prefix] then
        rest = strip_map_opts(rest)
        local lhs, rhs = rest:match("^(%S+)%s*(.*)$")
        if lhs then add(MAP_PREFIX[prefix] .. bang, lhs, vim.trim(rhs or "")) end
      elseif t:match("^com[mand]*!?%s") then
        local r = t:gsub("^%S+%s+", "")
        while r:match("^%-") do r = r:gsub("^%S+%s*", "") end
        local name, def = r:match("^(%S+)%s*(.*)$")
        if name then add("command", ":" .. name, def) end
      elseif t:match("^fu[nction]*!?%s") then
        local name, args = t:match("^%S+%s+([%w_#:.<>]+)%s*%((.-)%)")
        if name and name:match("^s:") then
          cur_fn = { notes = {}, body = {} } -- script-local helper: parse past it, don't document it
          pending = {}
        elseif name then
          cur_fn = add("function", name .. "(" .. (args or "") .. ")", "")
          cur_fn.body = {}
        end
      elseif t:match("^au[tocmd]*!?%s") then
        add("autocmd", t:gsub("^%S+%s+", ""), "")
      elseif t:match("^set%s") or t:match("^setlocal%s") or t:match("^setl%s") then
        local opts = t:gsub("%s%s+\".*$", ""):gsub("^%S+%s+", "")
        for opt in opts:gmatch("%S+") do table.insert(sec().settings, opt) end
        last = nil
      elseif t:match("^let%s") then
        local var, val = t:match("^let%s+([%w_:]+)%s*=%s*(.*)$")
        if var and not var:match("^[lsa]:") then table.insert(sec().vars, var .. " = " .. val) end
        last = nil
      else
        last = nil
      end
    end
  end
  flush_notes()
  return sections, order
end

local function sources(main)
  local base = expand(vim.g.MIMD_BASE or "~/echo-home/.vim/mimd")
  local files, seen = { main }, { [main] = true }
  local ok, lines = pcall(fn.readfile, main)
  if not ok then return files end
  for _, line in ipairs(lines) do
    local t = vim.trim(line)
    if not t:match('^"') then
      local m = t:match([[^exe[cute]*%s+'source%s+'%s*%.%s*g:MIMD_BASE%s*%.%s*"/([^"]+)"]])
      local path = m and (base .. "/" .. m)
      if not path then
        local s = t:match("^so[urce]*%s+(%S+)$")
        if s and not s:match("%.lua$") then path = expand(s) end
      end
      if path then
        for _, pat in ipairs(vim.g.neos_doc_skip or { "/vim%-plug/", "/bundle/", "/plugged/", "/autoload/", "/pack/" }) do
          if path:match(pat) then path = nil break end
        end
      end
      if path and not seen[path] and fn.filereadable(path) == 1 then
        seen[path] = true
        files[#files + 1] = path
      end
    end
  end
  return files
end

local function render_entry(e)
  if e.kind == "note" then return "- note: " .. table.concat(e.notes, "; ") end
  local s = ("- %s `%s`"):format(e.kind, e.name)
  if e.detail and e.detail ~= "" then s = s .. " → `" .. e.detail:gsub("`", "'") .. "`" end
  if #e.notes > 0 then s = s .. " — " .. table.concat(e.notes, "; ") end
  if e.body and #e.body > 0 then
    s = s .. " · body: " .. table.concat(e.body, " ⏎ "):gsub("`", "'"):sub(1, 240)
  end
  return s
end

function D.generate(out)
  local main = expand(vim.g.neos_vimrc or "~/.vimrc.neo")
  out = expand(out or vim.g.neos_doc_file or D.default_file)
  local files = sources(main)
  local doc = {
    "# Vim setup reference (generated by :NeosDoc)",
    ("Generated %s from %s and %d mini module(s). One entry per line; `map` = normal+visual+operator mode,")
      :format(os.date("%Y-%m-%d %H:%M"), short(main), #files - 1),
    "`→` shows what the key runs, `—` the user's own notes. Leader is `" .. (vim.g.mapleader or "\\") .. "`.",
    "",
  }
  local n_entries = 0
  for _, path in ipairs(files) do
    local sections, order = parse_file(path)
    if sections then
      for _, name in ipairs(order) do
        local s = sections[name]
        if #s.entries > 0 or #s.settings > 0 or #s.vars > 0 then
          doc[#doc + 1] = ("## %s  (%s)"):format(name, short(path))
          for _, e in ipairs(s.entries) do
            doc[#doc + 1] = render_entry(e)
            n_entries = n_entries + 1
          end
          if #s.settings > 0 then doc[#doc + 1] = "- settings: `set " .. table.concat(s.settings, " ") .. "`" end
          for _, v in ipairs(s.vars) do doc[#doc + 1] = "- variable `" .. v:gsub("`", "'") .. "`" end
          doc[#doc + 1] = ""
        end
      end
    end
  end
  local extra = vim.g.neos_doc_extra or { "~/echo-home/Documents/terminal-handbook/vim-rc.md" }
  for _, p in ipairs(extra) do
    local path = expand(p)
    if fn.filereadable(path) == 1 then
      doc[#doc + 1] = ("## HANDBOOK  (%s)"):format(short(path))
      for _, l in ipairs(fn.readfile(path)) do
        if l:match("^#+%s") then l = "### " .. l:gsub("^#+%s*", "") end
        doc[#doc + 1] = l
      end
      doc[#doc + 1] = ""
    end
  end
  fn.mkdir(fn.fnamemodify(out, ":h"), "p")
  fn.writefile(doc, out)
  return out, n_entries, #files
end

-- Regenerate when missing or older than any source file.
function D.ensure(out)
  out = expand(out or vim.g.neos_doc_file or D.default_file)
  local t = fn.getftime(out)
  if t < 0 then return D.generate(out) end
  for _, p in ipairs(sources(expand(vim.g.neos_vimrc or "~/.vimrc.neo"))) do
    if fn.getftime(p) > t then return D.generate(out) end
  end
  return out
end

return D
