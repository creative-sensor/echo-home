<#
.SYNOPSIS
    Configures Windows OpenSSH Server user authorized keys C:\Users\username\.ssh\authorized_keys for Administrators.
.DESCRIPTION
    This script creates the .ssh directory and authorized_keys file
    for a specified user, enforces the strict permissions required by OpenSSH, and restarts the SSH service.
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



# 3. Create .ssh folder and authorized_keys file
# Get the target user's profile path (handles cases where profile path isn't just C:\Users\Username)
$userSID = (New-Object System.Security.Principal.NTAccount($TargetUsername)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$userProfilePath = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$userSID").ProfileImagePath

$sshDirPath = Join-Path $userProfilePath ".ssh"
$authKeysPath = Join-Path $sshDirPath "authorized_keys"

if (-not (Test-Path $sshDirPath)) {
    New-Item -ItemType Directory -Force -Path $sshDirPath | Out-Null
    Write-Host "Created directory: $sshDirPath"
}

if (-not (Test-Path $authKeysPath)) {
    New-Item -ItemType File -Force -Path $authKeysPath | Out-Null
    Write-Host "Created file: $authKeysPath"
}

# 4. Optionally append the public key if provided
if (![string]::IsNullOrWhiteSpace($PublicKeyString)) {
    Add-Content -Path $authKeysPath -Value $PublicKeyString
    Write-Host "Appended public key to authorized_keys." -ForegroundColor Green
} else {
    Write-Host "No public key provided. Don't forget to paste your public key into $authKeysPath" -ForegroundColor Yellow
}

# 5. Fix File Permissions using icacls
Write-Host "Applying strict file permissions to authorized_keys..."
# Strip inherited permissions
icacls.exe $authKeysPath /inheritance:r | Out-Null
# Grant SYSTEM full control
icacls.exe $authKeysPath /grant "SYSTEM:(F)" | Out-Null
# Grant the target user full control
icacls.exe $authKeysPath /grant "${TargetUsername}:(F)" | Out-Null

Write-Host "Permissions updated successfully." -ForegroundColor Green

# 6. Restart the OpenSSH Service
Write-Host "Restarting sshd service..."
Restart-Service sshd -Force
Write-Host "SSH Service restarted. Setup complete!" -ForegroundColor Cyan
