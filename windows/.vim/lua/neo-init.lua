require("nvim-web-devicons")
require('lualine').setup(
--  {options = { theme = 'nord' }}
--  {options = { theme = 'papercolor_dark' }}
--  {options = { theme = 'solarized_dark' }}
--  {options = { theme = 'solarized_light' }}
)

local theme = {
  fill = 'TabLineFill',
  -- Also you can do this: fill = { fg='#f2e9de', bg='#907aa9', style='italic' }
  head = { fg='#ffffff', bg='#000000', style='italic' },
  current_tab = { fg='#000000', bg='#f5006e', style='italic' },
  tab = 'TabLine',
  win = 'TabLine',
  tail = 'TabLine',
}
require('tabby').setup(
  {
    line = function(line)
      return {
        {
          { ' W ', hl = theme.head },
          line.sep('', theme.head, theme.fill),
        },
        line.tabs().foreach(function(tab)
          local hl = tab.is_current() and theme.current_tab or theme.tab
          return {
            line.sep('', hl, theme.fill),
            tab.is_current(),
--            tab.name():gsub('%s*%[.+%]', ''),
            tab.number(),
--            tab.close_btn(''),
            line.sep('', hl, theme.fill),
            hl = hl,
            margin = ' ',
          }
        end),
        line.spacer(),
        hl = theme.fill,
      }
    end,
    option = {}
  }
)

require('peek').setup({
  auto_load = true,         -- whether to automatically load preview when entering another buffer
  close_on_bdelete = true,  -- close preview window on buffer delete
  syntax = true,            -- enable syntax highlighting, affects performance
  theme = 'light',           -- 'dark' or 'light'
  update_on_change = true,
  app = 'browser',          -- 'webview', 'browser', or table with command
  filetype = { 'markdown' },-- list of filetypes to recognize as markdown
  throttle_at = 2000,       -- throttle update min-delay at 2000ms
  throttle_timeout = 30     -- timeout for throttle
})

-- Create user commands to open and close the preview
-- vim.api.nvim_create_user_command('PeekOpen', require('peek').open, {})
-- vim.api.nvim_create_user_command('PeekClose', require('peek').close, {})
vim.keymap.set('n', 'peek', require('peek').open, { desc = "Open Peek Markdown Preview" })
vim.keymap.set('n', 'peeke', require('peek').close, { desc = "Close Peek Markdown Preview" })

-- Create a table of your custom capabilities
_G.AgentManifest = {
    keymaps = {
        ["tff"] = "Opens Telescope file finder",
        ["tgr"] = "Opens Troubleshooting quickfix list",
        ["e3"] = "Opens file path at depth 3",
        ["e3"] = "Opens file path at depth 3",
        ["ptrm"] = "Opens vimpage for Port Remote"
    },
    commands = {
        ["Evx"] = "Opens file path at depth X",
        ["FgXp"] = "Opens Field Git Explorer"
    },
    functions = {
        ["AdjustFontSize"] = "Increase with AdjustFontSize(1) or decrease with AdjustFontSize(-1)",
        ["VimPage_ptrm"] = "Open Vim Page for Port Remote"
    }
}

-- Create a command the LLM can call to print this as JSON
vim.api.nvim_create_user_command('AgentCapabilities', function()
    print(vim.fn.json_encode(_G.AgentManifest))
end, { desc = "Prints a JSON manifest of user-defined custom functions for the AI agent" })
