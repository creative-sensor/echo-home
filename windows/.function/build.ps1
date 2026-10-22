$ErrorActionPreference = "Stop"

$GITROOT = git rev-parse --show-toplevel

# ---- setup PATH ----
& "$PSScriptRoot\..\.pwsh.ps1"