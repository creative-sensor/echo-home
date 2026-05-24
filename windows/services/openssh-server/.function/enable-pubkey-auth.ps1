<#
.SYNOPSIS
    Configures Windows OpenSSH Server to use standard C:\Users\username\.ssh\authorized_keys for Administrators.
.DESCRIPTION
    This script modifies the sshd_config file
#>

param (
    # Defaults to the currently logged in user running the script
    [string]$TargetUsername = $env:USERNAME,
    
    # Optional: You can pass your public key string directly to append it to the file
    [string]$PublicKeyString = ""
)

# 1. Ensure the script is running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "This script must be run as an Administrator. Please launch PowerShell as Admin and try again."
    Exit
}

Write-Host "Starting SSH Public Key Configuration for user: $TargetUsername" -ForegroundColor Cyan

# 2. Modify sshd_config
$sshdConfigPath = "$env:ProgramData\ssh\sshd_config"
if (Test-Path $sshdConfigPath) {
    Write-Host "Updating sshd_config..."
    $config = Get-Content $sshdConfigPath

    # Ensure PubkeyAuthentication is uncommented and set to yes
    $config = $config -replace '^\s*#?\s*PubkeyAuthentication\s+no', 'PubkeyAuthentication yes'
    $config = $config -replace '^\s*#\s*PubkeyAuthentication\s+yes', 'PubkeyAuthentication yes'

    # Comment out the Match Group administrators block
    $config = $config -replace '(?m)^Match Group administrators', '#Match Group administrators'
    $config = $config -replace '(?m)^\s*AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys', '#       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'

    Set-Content -Path $sshdConfigPath -Value $config -Force
    Write-Host "Successfully updated $sshdConfigPath." -ForegroundColor Green
} else {
    Write-Error "sshd_config not found at $sshdConfigPath. Is the OpenSSH Server installed?"
    Exit
}


# 6. Restart the OpenSSH Service
Write-Host "Restarting sshd service..."
Restart-Service sshd -Force
Write-Host "SSH Service restarted. Setup complete!" -ForegroundColor Cyan
