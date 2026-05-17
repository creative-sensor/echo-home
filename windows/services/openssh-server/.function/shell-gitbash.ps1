New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name "DefaultShell" -Value "C:\Program Files\Git\bin\bash.exe" -PropertyType String -Force
Restart-Service sshd
