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
set ttymouse=sgr
set splitright
set title titlestring=VIM\ \|\ %{fnamemodify(getcwd(),\ ':t')} titlelen=32
    "output:  VIM | current_dirname
set background=light
set backspace=indent,eol,start
set ffs=unix
set noswapfile
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


"-------- [GLOBAL] --------
set shellslash
let g:cycle_dir = $DATA_DIR.'/'.$CYCLE
let g:VIEW = '0.VIEW'
let g:BACKLOG = '1.BACKLOG'
let g:TODO = '2.TODO'
let g:HOLD = '3.HOLD'
let g:WIP = '4.WIP'
let g:DONE= '5.DONE'
let g:tty_fg_color = $TTY_FG_COLOR
let g:tty_bg_color = $TTY_BG_COLOR

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
    exec 'file '.g:VIEW.' | read HELP.md | setlocal filetype=markdown'
        "vim: read file into buffer
    exec 'new '.g:BACKLOG
    exec 'silent read ! echo "----BACKLOG----"; find '.g:cycle_dir.'/BACKLOG -type f | sort '
    exec 'vnew '.g:TODO
    exec 'silent read ! echo "----TODO----"; find '.g:cycle_dir.'/TODO -type f | sort '
    exec 'vnew '.g:HOLD
    exec 'silent read ! echo "----HOLD----"; find '.g:cycle_dir.'/HOLD -type f | sort '
    exec 'vnew '.g:WIP
    exec 'silent read ! echo "----WIP----"; find '.g:cycle_dir.'/WIP -type f | sort '
    exec 'vnew '.g:DONE
    exec 'silent read ! echo "----DONE----"; find '.g:cycle_dir.'/DONE -type f | sort ' 

endfunction


function XCard(xname)
    let column_name = fnamemodify(bufname(),':e')
        " vim: extract file extension
    let yname = g:cycle_dir.'/'.column_name.'/'.a:xname.'.md'
    exec 'silent !cp CARD.md '.yname
    exec 'silent !git add '.yname
    exec 'silent read ! echo '.yname
endfunction
command! -nargs=* XCard call XCard(<q-args>)


function ViewCard(name)
    "cursor is presumed being at VIEW window already
    let xname = trim(a:name)
    exec "%delete"
    exec "silent read ! echo '>>>> Viewing for '".xname
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
    let yname = g:cycle_dir.'/'.column_name.'/'.card_short_name
    exec 'silent !set -x ; git add '.xname.';  git mv '.xname.' '.yname
    exec 'silent read ! echo '.yname
endfunction

function RefreshColumn()
    let column_name = fnamemodify(bufname(),':e')
    exec "%delete"
    exec 'silent read ! echo "----'.column_name.'----"; find '.g:cycle_dir.'/'.column_name.' -type f | sort'
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
  execute  "highlight Terminal ctermbg=".g:tty_bg_color." ctermfg=".g:tty_fg_color." guibg=#073642   guifg=#93a1a1 "
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
inoremap <F8><F8>  <C-o>: r! date "+\%Y-\%m-\%d.\%s"<CR>
    "temporarily switch to normal from insert mode and run command
map tty : call Terminator()<CR>


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

nnoremap  rr  :call RefreshColumn() <CR>
    " REFRESH CONTENT OF CURSORED COLUMN

"-------- [POST-INIT] --------
colorscheme solarized8_high
