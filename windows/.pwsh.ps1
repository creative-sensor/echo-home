$ErrorActionPreference = "Stop"

$LOCAL_BIN = "$env:USERPROFILE\.local\bin"
$GITROOT = git rev-parse --show-toplevel
$HOME_DIR = "$env:USERPROFILE"

[Environment]::SetEnvironmentVariable("LOCAL_BIN", $LOCAL_BIN, "User")

$pathEntries = @(
    "$LOCAL_BIN\python"
    "$LOCAL_BIN\python\Scripts"
    "C:\Program Files (x86)\Vim\vim90"
    "$LOCAL_BIN\marktext"
    "$HOME_DIR\echo-home\windows\.function"
    "$HOME_DIR\echo-home\.function"
    "$HOME_DIR\codev\yson\.function"
    "$env:PROGRAMFILES\Go\bin"
    "$LOCAL_BIN\node"
    "$LOCAL_BIN\gmlama"
    "$LOCAL_BIN\diffscp"
    "$LOCAL_BIN\vscodium"
    "$LOCAL_BIN\llvm-mingw\bin"
    "$LOCAL_BIN\rust\rustc\bin"
    "$LOCAL_BIN\rust\cargo\bin"
    "C:\Program Files\Neovim\bin"
    "C:\Program Files\Neovide"
    "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
    "$LOCAL_BIN\cmake\bin"
    "$LOCAL_BIN\xpack\bin"
    "$LOCAL_BIN\rsync\usr\bin"
    "$LOCAL_BIN\utilinux\usr\bin"
    "$LOCAL_BIN\lib-msys"
    "$LOCAL_BIN\terminal"
)

$currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
foreach ($entry in $pathEntries) {
    if (-not $currentPath.Contains($entry)) {
        $currentPath += ";$entry"
    }
}
[Environment]::SetEnvironmentVariable("PATH", $currentPath, "User")

Write-Host "PATH updated. Restart your shell to apply changes."