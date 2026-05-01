param(
    [Parameter(Mandatory=$true)]
    [string]$ServiceName
)

# Determine the base directory (windows/) to locate service overrides
# This assumes the script is run from a context where we can resolve the path
$BaseDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)

$OverridePath = Join-Path $BaseDir "services\$ServiceName\.function\status"

if (Test-Path $OverridePath) {
    Write-Host "Found override for $ServiceName at $OverridePath. Executing..."
    & $OverridePath
} else {
    Write-Host "No override found for $ServiceName. Fetching standard status..."
    try {
        $service = Get-Service -Name $ServiceName -ErrorAction Stop
        Write-Host "Service: $($service.Name)"
        Write-Host "Status:  $($service.Status)"
    } catch {
        Write-Error "Failed to retrieve status for service '$ServiceName': $($_.Exception.Message)"
        exit 1
    }
}
