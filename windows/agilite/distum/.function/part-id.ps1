#!/usr/bin/env powershell
$drive = $args[0]
#$drive
$drive_letter = @(echo $drive | grep -E -o '[a-zA-Z]+')
#$drive_letter

$disk_id = @(Get-Partition -DriveLetter $drive_letter | Select-Object  -ExpandProperty DiskId | awk -F'-' '{print $6}' | grep  -E  -o '[a-f0-9]+')
$part_number = @(Get-Partition -DriveLetter D | Select-Object -ExpandProperty PartitionNumber)

echo "$disk_id-$part_number"
