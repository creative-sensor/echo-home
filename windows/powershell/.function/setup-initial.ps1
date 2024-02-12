#Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

$PROFILE_CUCH = "$HOME\Documents\WindowsPowerShell"
if (-not (Test-Path -Path $PROFILE_CUCH)) { mkdir $PROFILE_CUCH }
cp Microsoft.PowerShell_profile.ps1 $PROFILE_CUCH
