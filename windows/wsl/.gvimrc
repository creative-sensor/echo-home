set guifont=Consolas:h11
let g:HOME_WSL="/mnt/c/Users/$USER/"
let g:TEMPNAME_WSL=g:HOME_WSL . "AppData/Local/Temp/"
map tty : highlight Terminal ctermbg=23 ctermfg=15 guibg=#073042 guifg=#7ec1de \| below terminal ++rows=7<CR>
    " Scroll in terminal: Ctrl w   Shift n

function WSL_set_shell()
    "Concept: wsl -- bash -c "find ./ | grep Pictures > file.wsl"
    set shell=\"C:\Windows\Sysnative\wsl.exe\"
    set shellpipe=|
    set shellredir=>
    set shellcmdflag=--
    set shellxquote=" "
    set shellslash
endfunction




