param(
    [Parameter(Mandatory=$true)]
    [string]$SshdConfigPath
)

# Check if the file exists
if (-not (Test-Path $SshdConfigPath)) {
    Write-Error "Error: SSHD configuration file not found at '$SshdConfigPath'"
    exit 1
}

Write-Host "Processing SSHD configuration file: $SshdConfigPath"

$setting = "PubkeyAuthentication yes"
$commentedSetting = "#PubkeyAuthentication yes"
$content = Get-Content $SshdConfigPath -Raw
$modified = $false
$actionTaken = ""

# Check if the setting is already present and uncommented
if ($content -match [regex]::Escape($setting)) {
    Write-Host "PubkeyAuthentication is already enabled in the configuration."
    exit 0
}
# Check if the commented setting exists, and if so, uncomment it
elseif ($content -match [regex]::Escape($commentedSetting)) {
    Write-Host "Found commented PubkeyAuthentication setting. Uncommenting..."
    
    # Replace the commented line with the active line
    $newContent = $content -replace [regex]::Escape($commentedSetting), $setting
    
    # Write back to the file
    $newContent | Set-Content $SshdConfigPath -Force
    $actionTaken = "Successfully updated $SshdConfigPath to enable PubkeyAuthentication."
    $modified = $true
}
# If neither is found, append it
else {
    Write-Warning "Could not find 'PubkeyAuthentication' setting. Appending it to the end of the file."
    $newContent = $content + "`n`n# --- Custom Configuration ---`n$setting"
    $newContent | Set-Content $SshdConfigPath -Force
    $actionTaken = "Successfully appended PubkeyAuthentication configuration to $SshdConfigPath."
    $modified = $true
}

if ($modified) {
    Write-Host "`n$actionTaken"
    Write-Host "Please remember to restart the SSH service for changes to take effect."
    Write-Host "You can run: Restart-Service sshd"
}
