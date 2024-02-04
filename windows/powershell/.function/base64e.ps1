
$Bytes = [System.Text.Encoding]::Unicode.GetBytes($args[0])
$EncodedText = [Convert]::ToBase64String($Bytes)
$EncodedText
