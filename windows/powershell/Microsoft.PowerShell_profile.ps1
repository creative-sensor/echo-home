function prompt{
#    Write-Host("$([char]0x264a)$([char]0x2648)" + $Env:UserName) -NoNewLine -ForegroundColor 2
    Write-Host("[" + $Env:UserName) -NoNewLine -ForegroundColor 2
    Write-Host("@" + $Env:ComputerName +"] ") -NoNewLine -ForegroundColor 5
    Write-Host([io.path]::GetFileNameWithoutExtension((pwd)) +">") -NoNewLine -ForegroundColor 6
    return " "
}

