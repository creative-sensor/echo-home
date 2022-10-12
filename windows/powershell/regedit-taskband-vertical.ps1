#Get-ExecutionPolicy
#Set-ExecutionPolicy RemoteSigned


Get-PSProvider
Get-PSDrive
$RegistryPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband'
$Name         = 'NumThumbnails'
$Value        = '0'
New-ItemProperty -Path $RegistryPath -Name $Name -Value $Value -PropertyType DWORD -Force 
