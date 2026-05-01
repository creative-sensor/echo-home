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

$content = Get-Content $SshdConfigPath -Raw

# Define the setting we want to ensure is active
$setting = "AllowTcpForwarding yes"
$commentedSetting = "#AllowTcpForwarding yes"
$modified = $false

# Check if the setting is already present and uncommented
if ($content -match [regex]::Escape($setting)) {
    Write-Host "Port forwarding is already enabled in the configuration."
    exit 0
}
# Check if the commented setting exists, and if so, uncomment it
elseif ($content -match [regex]::Escape($commentedSetting)) {
    Write-Host "Found commented setting for port forwarding. Uncommenting..."
    
    # Replace the commented line with the active line
    $newContent = $content -replace [regex]::Escape($commentedSetting), $setting
    
    # Write back to the file
    $newContent | Set-Content $SshdConfigPath -Force
    Write-Host "Successfully updated $SshdConfigPath."
    $modified = $true
}
# If neither is found, append it (though usually it's commented out)
else {
    Write-Warning "Could not find 'AllowTcpForwarding' setting. Appending it to the end of the file."
    $newContent = $content + "`n`n# --- Custom Configuration ---`n$setting"
    $newContent | Set-Content $SshdConfigPath -Force
    Write-Host "Successfully appended configuration to $SshdConfigPath."
    $modified = $true
}

if ($modified) {
    Write-Host "`nConfiguration updated successfully."
    Write-Host "Please remember to restart the SSH service for changes to take effect."
    Write-Host "You can run: Restart-Service sshd"
}
