"set shell=\"C:\Program\ Files\Git\usr\bin\bash.exe\"
set shell=\"C:\Program\ Files\Git\bin\sh.exe\"
set guifont=Consolas:h11
set shellslash
map tty : highlight Terminal ctermbg=23 ctermfg=15 guibg=#073042 guifg=#7ec1de \| below terminal ++rows=7<CR>
    " Scroll in terminal: Ctrl w   Shift n


function WSL_set_shell()
    "Concept: wsl -- bash -c "find ./ | grep Pictures > file.wsl"
    set shell=\"C:\Windows\Sysnative\wsl.exe\"
    set shellpipe=|
    set shellredir=>
    set shellcmdflag=--
    set shellxquote=" "
endfunction

function WSL_FgXp(pattern)
  " FIELD GIT EXPLORER
  call WSL_set_shell()
  let error_file_windows = ".\\tmp.quickfix"
  let error_file_wsl = "./tmp.quickfix"
  exe '!bash -c "find ./ -type f | grep -v "^./.git" | grep -i "'.a:pattern.'" | sed "s/\$/:1/" > '.error_file_wsl.'"'
  set errorformat=%f:%l
  exe "cfile ".error_file_windows
  copen
  call delete(error_file_windows)
endfun
command! -nargs=* FgXpWSL call WSL_FgXp(<q-args>)


