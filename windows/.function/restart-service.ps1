param(
    [Parameter(Mandatory=$true)]
    [string]$ServiceName
)

# Determine the base directory (windows/) to locate service overrides
# This assumes the script is run from a context where we can resolve the path
$BaseDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)

$OverridePath = Join-Path $BaseDir "services\$ServiceName\.function\restart"

if (Test-Path $OverridePath) {
    Write-Host "Found override for $ServiceName at $OverridePath. Executing..."
    & $OverridePath
} else {
    try {
        Restart-Service -Name $ServiceName -ErrorAction Stop
        Write-Host "Successfully restarted $ServiceName."
    } catch {
        Write-Error "Failed to restart service '$ServiceName': $($_.Exception.Message)"
        exit 1
    }
}
