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
set title titlestring=VIM\ \|\ %{fnamemodify(getcwd(),\ ':t')} titlelen=32
    "output:  VIM | current_dirname
set background=light
set backspace=indent,eol,start


"-------- [PLUGIN] --------

"-------- [PLUGIN / VUNDLE] --------
set nocompatible              " be iMproved, required
filetype off                  " required
set rtp+=~/.vim/bundle/Vundle.vim
call vundle#begin()
" let Vundle manage Vundle, required
Plugin 'VundleVim/Vundle.vim'
        "Plugin Manager
Plugin 'tpope/vim-fugitive'
        "Git commands
Plugin 'junegunn/gv.vim'
        "Git browser
Plugin 'lifepillar/vim-solarized8'
        "Colorscheme
Plugin 'jlanzarotta/bufexplorer'
        "Buffer Explorer
" All of your Plugins must be added before the following line
call vundle#end()            " required
filetype plugin indent on    " required
" Brief help
" :PluginList       - lists configured plugins
" :PluginInstall    - installs plugins; append `!` to update or just :PluginUpdate
" :PluginSearch foo - searches for foo; append `!` to refresh local cache
" :PluginClean      - confirms removal of unused plugins; append `!` to auto-approve removal



"-------- [FUNCTIONS] --------

function FgXp(pattern)
  " FIELD GIT EXPLORER
  let error_file = tempname()
  exe '!find "./" -type f | grep -v "^./.git" | grep -i "'  .  a:pattern  .  '" | sed "s/\$/:1/" > '.error_file
  set errorformat=%f:%l
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfunction
command! -nargs=* FgXp call FgXp(<q-args>)

function Kolumns()
    let yyyy_dir = "datum/".$ENV_DATE
    let VIEW = tempname().'.VIEW'
    let TODO = tempname().'.TODO'
    let HOLD = tempname().'.HOLD'
    let WIP = tempname().'.WIP'
    let DONE= tempname().'.DONE'
    exec 'file '.VIEW.' | read HELP.md | setlocal filetype=markdown'
        "vim: read file into buffer
    exec '! find '.yyyy_dir.'/TODO -type f > '.TODO | exec 'new '.TODO
    exec '! find '.yyyy_dir.'/HOLD -type f > '.HOLD | exec 'vnew '.HOLD
    exec '! find '.yyyy_dir.'/WIP -type f > '.WIP | exec 'vnew '.WIP
    exec '! find '.yyyy_dir.'/DONE -type f > '.DONE | exec 'vnew '.DONE
endfunction


function XCard(xname)
    let column_name = fnamemodify(bufname(),':e')
        " vim: extract file extension
    let yname = $DATA_DIR.'/'.column_name.'/'.a:xname.'.md'
    exec 'silent !cp CARD.md '.yname
    exec 'silent !git add '.yname
    exec 'silent read ! echo '.yname
endfunction
command! -nargs=* XCard call XCard(<q-args>)


function ViewCard(name)
    "cursor is presumed being at VIEW window already
    let xname = trim(a:name)
    exec "%delete"
    exec "silent read ".xname
    exec "setlocal filetype=markdown"
endfunction


function EditCard(name)
    let xname = trim(a:name)
    exec 'tabnew '.xname
    exec "setlocal filetype=markdown"
endfunction


function MoveCard(name)
    let xname = trim(a:name)
    let column_name = fnamemodify(bufname(),':e')
        " vim: extract file extension
    let card_short_name = fnamemodify(xname,':t')
        " vim: extract base name
    let yname = $DATA_DIR.'/'.column_name.'/'.card_short_name
    exec 'silent !git mv '.xname.' '.yname
    exec 'silent read ! echo '.yname
endfunction


fun! GrepSource(text)
  " GREP SOURCE CODE WHICH CONTAINS TEXT
  " :Grin text  " open window of search results
  " CTRLw gf    " open file in new tab
  let error_file = tempname()
  exe '!grep -rin '.a:text.' >  '.error_file
  set errorformat=%f:%l:%m
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfun
command! -nargs=1 Grin call GrepSource(<q-args>)



fun! Terminator()
  " Mini Terminal
  execute  "highlight Terminal ctermbg=23 ctermfg=15 guibg=#073642   guifg=#93a1a1 "
  execute  "below terminal ++rows=7"
endfun
command! -nargs=0 Terminator call Terminator()



"-------- [AUTOCMD] --------
autocmd FileType yaml  : set tabstop=2
autocmd VimEnter * silent call Kolumns()
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


vnoremap ===    "+y : vsplit <CR> : Grin <C-R>+ <CR>
    "LOOK UP KEYWORD
    ":copy selected text into register '+'
    ":vertical split window
    ":and call function Grin which take
    "content of '+' as argument

nnoremap  --   yy <C-w><Down>  :call ViewCard('<C-R>"') <CR>
    " copy current line at cursor to clipboard
    " view card at the path in clipboard

nnoremap  ---   yy :call EditCard('<C-R>"') <CR>
    " copy current line at cursor to clipboard
    " open card in new tab

nnoremap  mmm   dd  <C-w><Right>  :call MoveCard('<C-R>"') <CR>
    " copy current line at cursor to clipboard
    " delete current line
    " move cursor to the right
    " insert card where the cursor is

nnoremap  bbb   dd  <C-w><Left>  :call MoveCard('<C-R>"') <CR>
    " copy current line at cursor to clipboard
    " delete current line
    " move cursor to the right
    " insert card where the cursor is

"-------- [POST-INIT] --------
colorscheme solarized8_high