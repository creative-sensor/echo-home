#!/usr/bin/env powershell
$drive = $args[0]
#$drive
$drive_letter = @(echo $drive | grep -E -o '[a-zA-Z]+')
#$drive_letter
Get-Partition -DriveLetter $drive_letter | Select-Object -ExpandProperty Guid | awk -F'-' '{print $NF}' | grep  -E  -o '[a-f0-9]+'

