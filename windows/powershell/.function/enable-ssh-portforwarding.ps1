# Script to enable SSH port forwarding in OpenSSH Server on Windows

# Define the path to the SSH server configuration file
$SshdConfigPath = "C:\ProgramData\ssh\sshd_config"

# Define the configuration directives to ensure are set
$Directives = @(
    "AllowTcpForwarding yes",
    "GatewayPorts yes" # Optional, but often useful when enabling forwarding
)

Write-Host "Checking SSH server configuration at: $SshdConfigPath"

# Check if the configuration file exists
if (-not (Test-Path $SshdConfigPath)) {
    Write-Error "SSH configuration file not found at $SshdConfigPath. Please ensure OpenSSH Server is installed correctly."
    exit 1
}

# Function to update or add a directive in the config file
function Update-SshdConfig {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Directive
    )
    
    Write-Host "Ensuring directive is set: $Directive"
    
    # Read all lines from the config file
    $content = Get-Content $SshdConfigPath
    $updatedContent = @()
    $directiveFound = $false

    foreach ($line in $content) {
        # Check if the line contains the directive (ignoring comments and whitespace)
        if ($line -match "^\s*#?\s*$($Directive.Split(' ')[0])") {
            # If found, replace it with the correct setting
            $updatedContent += $Directive
            $directiveFound = $true
        } elseif ($line -match "^\s*#?\s*$($Directive.Split(' ')[0])\s+no") {
            # If found but set to 'no', replace it
            $updatedContent += $Directive
            $directiveFound = $true
        } else {
            $updatedContent += $line
        }
    }

    # If the directive was not found, append it to the end of the file
    if (-not $directiveFound) {
        Write-Host "Directive not found. Appending to $SshdConfigPath."
        $updatedContent += "`n# Added by script for port forwarding"
        $updatedContent += $Directive
    }

    # Write the updated content back to the file
    $updatedContent | Set-Content $SshdConfigPath -Force
    Write-Host "Configuration updated successfully."
}

# Apply all required directives
foreach ($directive in $Directives) {
    Update-SshdConfig -Directive $directive
}

# Restart the SSH service to apply changes
Write-Host "Restarting SSH Server service..."
try {
    Restart-Service sshd -Force
    Write-Host "SSH Server service restarted successfully. Port forwarding should now be enabled."
} catch {
    Write-Error "Failed to restart SSH Server service. Ensure the service is installed and running. Error: $($_.Exception.Message)"
    exit 1
}
```
```