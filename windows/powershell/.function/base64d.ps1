$DecodedText = [System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String($args[0]))
$DecodedText
