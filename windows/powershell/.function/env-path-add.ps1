function add($p) {
    $Path = [Environment]::GetEnvironmentVariable( "Path", "Machine" )
    $PathList = $Path.Split([IO.Path]::PathSeparator)
    foreach ($item in $PathList) {
        if ( $item -eq $p ) {
            echo "`"$item`"  existing"
            exit 0
        }
    }
    $Path = $Path + [IO.Path]::PathSeparator + $p
    $Env:PATH = $Path
    $Env:PATH.split(";")
    [Environment]::SetEnvironmentVariable( "Path", $Path , "Machine" )
}

add  $args[0]
