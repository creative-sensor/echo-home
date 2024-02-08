"set shell=\"C:\Program\ Files\Git\usr\bin\bash.exe\"
set shell=\"C:\Program\ Files\Git\bin\sh.exe\"
set guifont=Consolas:h11
set shellslash
let g:HOME_WSL="/mnt/c/Users/$USER/"
map tty : highlight Terminal ctermbg=23 ctermfg=15 guibg=#073042 guifg=#7ec1de \| below terminal ++rows=7<CR>
    " Scroll in terminal: Ctrl w   Shift n
map wsl : call WSL_set_shell()<CR>
    " Use WSL shell

function WSL_set_shell()
    "Concept: wsl -- bash -c "find ./ | grep Pictures > file.wsl"
    set shell=\"C:\Windows\Sysnative\wsl.exe\"
    set shellpipe=|
    set shellredir=>
    set shellcmdflag=--
    set shellxquote=" "
endfunction




