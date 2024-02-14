function rm-path($p) {
    $Path = [Environment]::GetEnvironmentVariable( "Path", "Machine" )
    $Path = $Path -replace [regex]::Escape(";*$p;*"), ""
    $Path = $Path -replace [regex]::Escape(";$p"), ""
    $Path = $Path -replace [regex]::Escape("$p;"), ""
    $Env:PATH = $Path
    $Env:PATH.split(";")
    [Environment]::SetEnvironmentVariable( "Path", $Path , "Machine" )
}

rm-path $args[0]
