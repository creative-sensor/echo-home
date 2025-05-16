 Get-Partition | Select-Object DriveLetter,Guid | ConvertTo-Json -Depth 1
 Get-Partition -DriveLetter D | Select-Object Guid  |  ConvertTo-Json -Depth 1 | jq -r .Guid
