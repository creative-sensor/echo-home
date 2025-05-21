#!/usr/bin/env powershell
$drive = $args[0]
#$drive
$drive_letter = @(echo $drive | grep -E -o '[a-zA-Z]+')
#$drive_letter
Get-Partition -DriveLetter $drive_letter | Select-Object Guid  |  ConvertTo-Json -Depth 1 | jq -r .Guid
