set laststatus=2
set ruler
set cursorline
set hlsearch
set nowrap
set expandtab
set tabstop=4
set mouse=a

filetype plugin on
syntax on
"highlight CursorLine cterm=NONE ctermbg=24
highlight CursorLine cterm=NONE ctermbg=251

let g:netrw_browse_split = 3
    "netrw open file in a new tab


" VUNDLE PLUGIN """""""""""""""""""""""""""""
set nocompatible              " be iMproved, required
filetype off                  " required
 set rtp+=~/.vim/bundle/Vundle.vim
 call vundle#begin()
" " let Vundle manage Vundle, required
 Plugin 'VundleVim/Vundle.vim'
 Plugin 'vim-scripts/taglist.vim'
 Plugin 'jreybert/vimagit'
 Plugin 'gregsexton/gitv'
 Plugin 'gabrielelana/vim-markdown'
 Plugin 'godlygeek/tabular'
 Plugin 'tpope/vim-fugitive'
 call vundle#end()            " required
 filetype plugin indent on    " required
""""""""""""""""""""""""""""""""""""""""""""""


 
" FIND FILES AND POPULATE THE QUICKFIX LIST
fun! FindGrep(filename)
  let error_file = tempname()
  exe '!find "./"| grep -i "'.a:filename.'" | xargs file | sed "s/:/:1:/" > '.error_file
  set errorformat=%f:%l:%m
  exe "cfile ". error_file
  copen
  call delete(error_file)
endfun
command! -nargs=1 FindGrep call FindGrep(<q-args>)




"""""""""""""""""""""""""""""""""""""""""""""""
"
" FUNCTION TO SEARCH INDEX IN ADM-NOTES
"
"""""""""""""""""""""""""""""""""""""""""""""""
function SearchIndex(...)
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
