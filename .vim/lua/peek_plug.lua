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
-- Simple keymap to build Peek manually
-- Mapped to 'peekb'
vim.keymap.set('n', 'peekb', ':!cd $LOCALAPPDATA/nvim-data/plugged/peek.nvim && deno task --quiet build<CR>', { desc = "Build Peek" })
