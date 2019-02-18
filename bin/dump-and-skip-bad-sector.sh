#!/bin/bash

DEV=$1
SNAPSHOT=$2
PART=0
ERROR_MSG=
SKIP=0
IO_ERR=1

#if test -f $SNAPSHOT ; then
#	echo "$SNAPSHOT exist"
#	exit 1
#fi

while [ $IO_ERR == 1 ]; do
	ERROR_MSG=`dd if=$DEV of=${SNAPSHOT} skip=$SKIP seek=$SKIP 2>&1`
	IO_ERR=$?
	COPIED_SECTORS=$(( `echo $ERROR_MSG | grep -o  'out [0-9]\+ bytes' | grep  -o '[0-9]\+'` / 512 ))
	if [ $COPIED_SECTORS == 0 ] ; then
		SKIP=$(( SKIP + 1 ))
		echo "$SKIP: bad sector"
	else
		SKIP=$(( SKIP + COPIED_SECTORS ))
	fi
done




