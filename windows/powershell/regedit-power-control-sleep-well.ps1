#Get-ExecutionPolicy
#Set-ExecutionPolicy RemoteSigned


Get-PSProvider
Get-PSDrive
$RegistryPath = 'HKLM:\SYSTEM\CurrentControlSet\Control\Power'
$Name         = 'CsEnabled'
$Value        = '0'
New-ItemProperty -Path $RegistryPath -Name $Name -Value $Value -PropertyType DWORD -Force 
