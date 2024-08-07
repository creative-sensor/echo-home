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
set paste
set title titlestring=VIM\ \|\ %{fnamemodify(getcwd(),\ ':t')} titlelen=32
    "output:  VIM | current_dirname
set background=light
set guifont=Consolas:h11
"set shell=\"/c/Program\ Files/Git/usr/bin/bash.exe\"
set shell=\"/c/Program\ Files/Git/bin/sh.exe\"
set backspace=indent,eol,start
set ffs=unix
set foldlevelstart=10
let g:yaml_revealer_separator='➤'
    "<C-v>u27a4
"-------- [PLUGIN] --------
"filetype plugin on

""NETRW
"let g:netrw_browse_split = 3
"    "netrw open file in a new tab
"let g:netrw_altv = 1
"let g:netrw_winsize = 25
"augroup ProjectDrawer
"  autocmd!
"  autocmd VimEnter * :Vexplore
"augroup END


"-------- [PLUGIN / VUNDLE] --------
set nocompatible              " be iMproved, required
filetype off                  " required
set rtp+=~/.vim/bundle/Vundle.vim
call vundle#begin()
" let Vundle manage Vundle, required
Plugin 'VundleVim/Vundle.vim'
        "Plugin Manager
Plugin 'thaerkh/vim-workspace'
        "Workspace
Plugin 'jreybert/vimagit'
        "Git workflow
Plugin 'tpope/vim-fugitive'
        "Git commands
Plugin 'junegunn/gv.vim'
        "Git browser
Plugin 'lifepillar/vim-solarized8'
        "Colorscheme
Plugin 'jlanzarotta/bufexplorer'
        "Buffer Explorer
Plugin 'Einenlum/yaml-revealer'
        "echo yaml path'
Plugin 'pedrohdz/vim-yaml-folds'
Plugin 'Yggdroot/indentLine'
Plugin 'dstein64/vim-menu'
Plugin 'equal-l2/vim-base64'

" All of your Plugins must be added before the following line
call vundle#end()            " required
filetype plugin indent on    " required
" Brief help
" :PluginList       - lists configured plugins
" :PluginInstall    - installs plugins; append `!` to update or just :PluginUpdate
" :PluginSearch foo - searches for foo; append `!` to refresh local cache
" :PluginClean      - confirms removal of unused plugins; append `!` to auto-approve removal



"-------- [FUNCTIONS] --------

fun! FindJsonPath()
  " FIND JSON PATH
  let path_file = tempname()
  let path_file_wsl = g:TEMPNAME_WSL . fnamemodify(path_file,':t')
  let buf_name = bufname()
  exe '!bash -c "find-jsonpath $(readlink -f '.buf_name.') > '.path_file_wsl.'"'
  exec 'vsplit '.path_file
  exec 'set filetype=yaml'
  call delete(path_file)
endfun
command! -nargs=0 Jpath call FindJsonPath()


fun! FindYamlPath()
  " FIND YAML PATH
  let path_file = tempname() . '.yaml'
  let path_file_wsl = g:TEMPNAME_WSL . fnamemodify(path_file,':t')
  let buf_name = bufname()
  exe '!bash -c "find-yamlpath $(readlink -f '.buf_name.') > '.path_file_wsl.'"'
  exe 'vsplit '.path_file
  call delete(path_file)
endfun
command! -nargs=0 Ypath call FindYamlPath()


fun! FindGrep(filename)
  " FIND FILES AND POPULATE THE QUICKFIX LIST
  let error_file = tempname()
  let error_file_wsl = g:TEMPNAME_WSL . fnamemodify(error_file,':t')
  exe '!bash -c "find ./| grep -i \"'.a:filename.'\" | xargs file | sed \"s/:/:1:/\" > '.error_file_wsl. '"'
  set errorformat=%f:%l:%m
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfun
command! -nargs=1 FindGrep call FindGrep(<q-args>)


function FgXp(pattern,maxdepth=32)
  " FIELD GIT EXPLORER
  let error_file_windows = tempname()
  let error_file_wsl = g:TEMPNAME_WSL . fnamemodify(error_file_windows,':t')
  silent exe '!bash -c "find ./ -maxdepth ' . a:maxdepth . ' -type f | grep -v "^./.git" | grep -i "'.a:pattern.'" | sed "s/\$/:1/" > '.error_file_wsl.'"'
  set errorformat=%f:%l
  silent exe "cfile ".error_file_windows
  copen
  call delete(error_file_windows)
endfun
command! -nargs=* FgXp call FgXp(<q-args>)

function Base64e()
  " BASE64 ENCODING
  let file = bufname()
  let file_b64 = file."__base64"
  silent exec '!bash -c "base64 <'.file.' |  tr -d -t \"\n\" > '.file_b64.'"'
  silent exec 'vnew '.file_b64
  call delete(file_b64)
endfunction

function EVsplitX(pattern,maxdepth=3)
  vsplit
  call FgXp(a:pattern,a:maxdepth)
endfunction
command! -nargs=* Evx call EVsplitX(<f-args>)


fun! GrepSource(text)
  " GREP SOURCE CODE WHICH CONTAINS TEXT
  " :Grin text  " open window of search results
  " CTRLw gf    " open file in new tab
  let error_file = tempname()
  let error_file_wsl = g:TEMPNAME_WSL . fnamemodify(error_file,':t')
  exe '!bash -c "grep -rin '.a:text.' >  '.error_file_wsl .'"'
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

function SessionMgmt()
    let limit = 512
    "exec "! set -x ; rm $(find Session.vim -mtime +7)"
    silent exec "! [[ ".limit." -lt $(wc -l Session.vim  | awk '{print $1}') ]] && rm Session.vim"
endfunction

function StartUp()
endfunction

"-------- [AUTOCMD] --------
autocmd FileType yaml  : set tabstop=2
"autocmd VimEnter * above terminal ++rows=7
"autocmd VimEnter * if isdirectory(".git") | silent call FgXp(".") | endif
"autocmd VimEnter * silent call SessionMgmt()
autocmd VimEnter * silent call WSL_set_shell()
autocmd TerminalOpen * set termwinsize=0*0
    "termwinsize: auto-adjustable
autocmd InsertEnter * highlight CursorLine ctermbg=190 guibg=#D5EE16 | highlight StatusLine ctermbg=190 guibg=#CCFF00
autocmd InsertLeave * highlight CursorLine ctermbg=254 guibg=#eee8d5 | highlight StatusLine ctermbg=254 guibg=#eee8d5




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
map q1 : q!<CR>
    "Quit current

map tty : highlight Terminal ctermbg=23 ctermfg=15 guibg=#073042 guifg=#7ec1de \| below terminal ++rows=7<CR>

map e3 : vsplit \| call FgXp(".",3)<CR>
map e6 : vsplit \| call FgXp(".",6)<CR>
map e9 : vsplit \| call FgXp(".",9)<CR>
    "List file at maxdepth=X

map e. : edit . <CR>
map t. : tabnew . <CR>
map s. : split . <CR>
map v. : vsplit . <CR>
map dff : windo diffthis<CR>
map dfg : diffget<CR>
map b64 : call Base64e()<CR>


"map <C-I> : tabnew . <CR>

vnoremap ccp    "+y
vnoremap ===    "+y : vsplit <CR> : Grin <C-R>+ <CR>
    "LOOK UP KEYWORD
    ":copy selected text into register '+'
    ":vertical split window
    ":and call function Grin which take
    "content of '+' as argument

vnoremap --    "+y : vsplit <C-R>+ <CR>
    "JUMP TO PATH

nnoremap <F5><F5> : edit<CR>

"-------- [POST-INIT] --------
colorscheme solarized8_high
