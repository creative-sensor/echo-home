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
set foldlevelstart=10
let g:yaml_revealer_separator='▶'


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
Plugin 'hashivim/vim-terraform'
        "Terraform highlight
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
  let @j = expand("%:p")
  exec '!find-jsonpath '.getreg('j').' > '.path_file
  exec 'vsplit '.path_file
  exec 'set filetype=yaml'
  call delete(path_file)
endfun
command! -nargs=0 Jpath call FindJsonPath()


fun! FindYamlPath()
  " FIND YAML PATH
  let path_file = tempname() . '.yaml'
  let @y = expand("%:p")
  exe '!find-yamlpath '.getreg('y').' > '.path_file
  exe 'vsplit '.path_file
  call delete(path_file)
endfun
command! -nargs=0 Ypath call FindYamlPath()


fun! FindGrep(filename)
  " FIND FILES AND POPULATE THE QUICKFIX LIST
  let error_file = tempname()
  exe '!find "./"| grep -i "'.a:filename.'" | xargs file | sed "s/:/:1:/" > '.error_file
  set errorformat=%f:%l:%m
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfun
command! -nargs=1 FindGrep call FindGrep(<q-args>)


function FgXp(pattern, maxdepth=32)
  " FIELD GIT EXPLORER
  let error_file = tempname()
  exe '!find "./" -maxdepth ' . a:maxdepth . ' -type f | grep -v "^./.git" | grep -i "'  .  a:pattern  .  '" | sed "s/\$/:1/" > '.error_file
  set errorformat=%f:%l
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfunction
command! -nargs=* FgXp call FgXp(<q-args>)

function Base64e()
  " BASE64 ENCODING
  let file = bufname()
  exec "vnew ".file."__base64"
  exec 'silent read !base64 <'.file.'| tr -d -t "\n"'
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
  exe '!grep -rin '.a:text.' >  '.error_file
  set errorformat=%f:%l:%m
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfun
command! -nargs=1 Grin call GrepSource(<q-args>)
function Menus()
    exec 'menu KEYDEX.Git.STATUS    :Git <CR>tty<CR><C-w>L<C-w>='
    exec 'menu KEYDEX.Git.glog    :Gclog<CR>'
    exec 'menu KEYDEX.Git.log-graph    :GV<CR>'
    exec 'menu KEYDEX.Git.add    s'
    exec 'menu KEYDEX.Git.unstage    u'
    exec 'menu KEYDEX.Git.diffCursor    ='
    exec 'menu KEYDEX.Git.checkout<Tab>co\<space\>    co<Space>'
    exec 'menu KEYDEX.Git.branch<Tab>cb\<space\>    cb<Space>'
    exec 'menu KEYDEX.Git.commit-UP<Tab>cup    cup'
    exec 'menu KEYDEX.Git.commit-amend<Tab>ca    ca'
    exec 'menu KEYDEX.Git.commit<Tab>cc    cc'
    exec 'menu KEYDEX.Git.rbia    :Git rbia '
    exec 'menu KEYDEX.Utils.base64e<Tab>b64    b64'
    exec 'menu KEYDEX.Utils.base64d<Tab>b94    b94'
    exec 'menu KEYDEX.FgXp.3<Tab>e3    e3'
    exec 'menu KEYDEX.FgXp.6<Tab>e6    e6'
    exec 'menu KEYDEX.FgXp.9<Tab>e9    e9'
endfunction


function SearchIndex(...)
    " SEARCH INDEX IN ADM-NOTES
    if @% == ""
        echo "Select window to search first!"
        return
    endif

    "Convert keywords into index string
    let keyword_list = a:000
    let index_string = a:1
    for i in keyword_list[1:]
            let index_string = index_string . "-" . i
    endfor

    "Build regex: \(^#KEYWORD\)\|\(-KEYWORD\)\|\(-KEYWORD-\)\|\(, KEYWORD \)
    let myregex_head = "\\(^#" . index_string . "\\)\\|"
    let myregex_case1 = "\\(-" . index_string . "\\)\\|"
    let myregex_case2 = "\\(-" . index_string . "-\\)\\|"
    let myregex_case3 = "\\(, " . index_string . "\\)"
    let myregex = myregex_head . myregex_case1 . myregex_case2 . myregex_case3

    "Search by index string
    "hint: %:p means absolute path of file loaded in current window
    execute "vimgrep /". myregex . "/g" . " " . expand('%:p')

    "Open quickfix window for search results
    execute "cw"
endfunction
command -nargs=* SearchIndex call SearchIndex (<f-args>)

fun! Terminator()
  " Mini Terminal
  execute  "highlight Terminal ctermbg=23 ctermfg=15 guibg=#073642   guifg=#93a1a1 "
  execute  "below terminal ++rows=7"
endfun
command! -nargs=0 Terminator call Terminator()

function SessionMgmt()
    let limit = 512
    silent exec "! [[ ".limit." -lt $(wc -l Session.vim  | awk '{print $1}') ]] && rm Session.vim"
endfunction


"-------- [AUTOCMD] --------
autocmd FileType yaml  : set tabstop=2
autocmd BufRead,BufNewFile *.tf set filetype=tf
autocmd BufRead,BufNewFile *.hcl set filetype=tf
"autocmd VimEnter * above terminal ++rows=7
"autocmd VimEnter * if isdirectory(".git") | silent call FgXp(".") | endif
autocmd VimEnter * silent call SessionMgmt()
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

map tty : highlight Terminal ctermbg=23 ctermfg=254 guibg=#073042 guifg=#7ec1de \| below terminal ++rows=13<CR>

map e3 : vsplit \| call FgXp(".",3)<CR>
map e5 : vsplit \| call FgXp(".",5)<CR>
map e7 : vsplit \| call FgXp(".",7)<CR>
map e9 : vsplit \| call FgXp(".",9)<CR>
    "List file at maxdepth=X

map e. : edit . <CR>
map f5 : edit <CR>
map t. : tabnew . <CR>
map s. : split . <CR>
map v. : vsplit . <CR>
map dff : windo diffthis<CR>
map dfg : diffget<CR>
map b64 : call Base64e()<CR>
map mnm : call Menus()<CR>
map mn : Menu<CR>

vmap b64    \atob<Esc>
vmap b94    \btoa<Esc>

nnoremap cup : Git commit -m "up"<CR>

vnoremap ===    "+y : vsplit <CR> : Grin <C-R>+ <CR>
    "LOOK UP KEYWORD
    ":copy selected text into register '+'
    ":vertical split window
    ":and call function Grin which take
    "content of '+' as argument

vnoremap --    "+y : vsplit <C-R>+ <CR>
    "JUMP TO PATH

    
"-------- [POST-INIT] --------
colorscheme solarized8_high
