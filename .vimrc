""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"------ [MISC]
syntax on
highlight CursorLine cterm=NONE ctermbg=251
    "highlight CursorLine cterm=NONE ctermbg=24
filetype on



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"------ [SET]
set laststatus=2
set ruler
set cursorline
set hlsearch
set nowrap
set expandtab
set tabstop=4
set mouse=a



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"----- [PLUGIN]
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


"----- [PLUGIN / VUNDLE]
"set nocompatible              " be iMproved, required
"filetype off                  " required
"set rtp+=~/.vim/bundle/Vundle.vim
"call vundle#begin()
"Plugin 'VundleVim/Vundle.vim'
"Plugin 'tpope/vim-fugitive'
"Plugin 'git://git.wincent.com/command-t.git'
"Plugin 'file:///home/gmarik/path/to/plugin'
"Plugin 'rstacruz/sparkup', {'rtp': 'vim/'}
"Plugin 'jreybert/vimagit'
"call vundle#end()            " required
"filetype plugin indent on    " required



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"------ [FUNCTIONS]
"
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



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"------ [AUTOCMD]
"autocmd BufNewFile * :set filetype=txt
autocmd FileType yaml  : set tabstop=2
"autocmd VimEnter *      : let g:netrw_winsize=25
"autocmd VimEnter *      : Vexplore



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
"------ [KEYMAP]
map T : tabnew . <CR>
map C : s/^/#/<CR>
map uc : s/^#//<CR>
map Bp : bp<CR>
map Bn : bn<CR>
"map <C-I> : tabnew . <CR>

vnoremap --    "+y : Grin <C-R>+ <CR>
    "copy selected text into register '+' and call function Grin which take
    "content of '+' as argument
