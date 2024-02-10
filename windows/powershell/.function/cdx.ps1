#function cdx {
#    # teleport to directory
#    local DEST=$1
#    local CACHE=$HOME/.local/cdx_cache
#    test -z "$DEST"  &&  cat $CACHE | sort  &&  read -p 'DEST ?= ' DEST
#    count=$(grep -c -i "$DEST.*$" $CACHE)
#    [[ $count -gt 1 ]]  &&  grep -i "$DEST.*$" $CACHE  &&  read -p 'DEST ?= ' DEST  &&  cd $DEST
#    [[ $count -eq 1 ]]  &&  DEST=$(grep -i "$DEST.*$" $CACHE)  &&  cd $DEST
#    [[ $count -eq 0 ]]  &&  cd $DEST && echo $(pwd) >> $CACHE
#}

$DEST = $args[0]
$CACHE = $HOME/.local/cdx_cache.ps1
