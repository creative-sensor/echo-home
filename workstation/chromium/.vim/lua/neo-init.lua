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
  current_tab = { fg='#ffffff', bg='#0039f5', style='italic' },
  tab = 'TabLine',
  win = 'TabLine',
  tail = 'TabLine',
}
require('tabby').setup(
  {
    line = function(line)
      return {
        {
          { '  ', hl = theme.head },
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
