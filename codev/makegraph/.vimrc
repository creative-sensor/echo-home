"-------- [MISC] --------
syntax on
highlight CursorLine cterm=NONE ctermbg=251
    "highlight CursorLine cterm=NONE ctermbg=24
filetype on



"-------- [VARSET] --------
set laststatus=2
set ruler
set cursorline
set hlsearch
set nowrap
set expandtab
set tabstop=4
set mouse=a
set splitright
set title titlestring=%{fnamemodify(getcwd(),\ ':t')}\ \|\ node-io titlelen=32
    "output:  VIM | current_dirname
set background=light
set backspace=indent,eol,start
set ffs=unix


"-------- [PLUGIN] --------

"-------- [PLUGIN / VUNDLE] --------
set nocompatible              " be iMproved, required
filetype off                  " required
set rtp+=~/.vim/bundle/Vundle.vim
call vundle#begin()
" let Vundle manage Vundle, required
Plugin 'VundleVim/Vundle.vim'
        "Plugin Manager
Plugin 'lifepillar/vim-solarized8'
        "Colorscheme
Plugin 'jlanzarotta/bufexplorer'
        "Buffer Explorer
" All of your Plugins must be added before the following line
call vundle#end()            " required
filetype plugin indent on    " required



"-------- [FUNCTIONS] --------

function Node_IO()
  let error_file = tempname()
  exe '!find "./" -type f  | grep -i "'  .$GRAPH_DIR.  '/node_.*" | sed "s/\$/:1/" > '.error_file
  set errorformat=%f:%l
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfunction

function Node_Function()
  let error_file = tempname()
  exe '!set -x ;find "./" -type f  | grep -i '$GRAPH_DIR.'/.*'.$REGEX.' |  sed "s/\$/:1/" > '.error_file
  set errorformat=%f:%l
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfunction

function Node_Log()
  let error_file = tempname()
  exe '!find "./" -type f  | grep -i "'  .$GRAPH_DIR.  '/.*.log$" |  sed "s/\$/:1/" > '.error_file
  set errorformat=%f:%l
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfunction


fun! Terminator()
  " Mini Terminal
  execute  "highlight Terminal ctermbg=23 ctermfg=15 guibg=#073642   guifg=#93a1a1 "
  execute  "below terminal ++rows=7"
endfun
command! -nargs=0 Terminator call Terminator()



"-------- [AUTOCMD] --------
autocmd FileType yaml  : set tabstop=2
"autocmd VimEnter * silent call Node_IO()
autocmd TerminalOpen * set termwinsize=0*0
    "termwinsize: auto-adjustable




"-------- [KEYMAP] --------
map T : tabnew . <CR>
    "New tab
map C : s/^/#/<CR>
    "Comment
map uc : s/^#//<CR>
    "Uncomment
map Bp : bp<CR>
    "Buffer back
map Bn : bn<CR>
    "Buffer next
map aC : :%!column -t<CR>
    "Format as columns
map WW : wa<CR>
    "Save all
map QQ : wqa<CR>
    "Save and quit all
map q0 : qa!<CR>
    "Quit all
map tty : highlight Terminal ctermbg=23 ctermfg=15 guibg=darkblue guifg=lightgrey \| below terminal ++rows=7<CR>




"-------- [POST-INIT] --------
colorscheme solarized8_high
