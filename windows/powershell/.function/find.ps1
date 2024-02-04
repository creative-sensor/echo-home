
$list = Get-ChildItem -Recurse $args[0] | Where {! $_.PSIsContainer } | Select FullName
foreach ($i in $list) { $i.FullName }
