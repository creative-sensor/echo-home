-- bootstrap lazy.nvim, LazyVim and your plugins
require("config.lazy")


require('solarized').set()
vim.o.background = "light"
vim.cmd([[colorscheme solarized]])
