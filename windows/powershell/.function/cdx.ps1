#function cdx {
#    # teleport to directory
#    local DEST=$1
#    local CACHE=$HOME/.local/cdx_cache
#    test -z "$DEST"  &&  cat $CACHE | sort  &&  read -p 'DEST ?= ' DEST
#    count=$(grep -c -i "$DEST.*$" $CACHE)
#    [[ $count -gt 1 ]]  &&  grep -i "$DEST.*$" $CACHE  &&  read -p 'DEST ?= ' DEST  &&  cd $DEST
#    [[ $count -eq 1 ]]  &&  DEST=$(grep -i "$DEST.*$" $CACHE)  &&  cd $DEST
#    [[ $count -eq 0 ]]  &&  cd $DEST && echo $(pwd) >> $CACHE

$DEST = $args[0]
$CACHE = "$HOME\.local\cdx_cache.ps1"

#touch
echo $null >> $CACHE

if ( [string]::IsNullOrEmpty($DEST) ) {
  if (Test-Path $CACHE) {
    cat $CACHE
    $DEST = Read-Host -Prompt 'DEST ?= '
    cd $DEST
  }
} else {
  $count = 0
  $currentLine = "."
  $DEST_regex = $DEST.replace("\","\\")
  foreach($line in Get-Content $CACHE) {
    if($line -match "$DEST_regex"){
      echo $line
      $currentLine = $line
      $count++
    }
  }
  if ( $count -eq 1 ) { cd $currentLine }
  if ( $count -eq 0 ) { cd $DEST;  if($?){echo $pwd.path >> $CACHE} }
  if ( $count -gt 1 ) { $DEST = Read-Host -Prompt 'DEST ?= '; cd $DEST }
}



