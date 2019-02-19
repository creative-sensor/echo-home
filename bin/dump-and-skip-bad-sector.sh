#!/bin/bash -e

###############################################################
# I can dump entire a block device into a normal snapshot file
# Use me as a backup or snapshot tool. Good luck!
###############################################################


DEV=$1
SNAPSHOT=$2



### Check Snapshot
if test  -b $SNAPSHOT || test -f $SNAPSHOT ; then
	echo "$SNAPSHOT already exist or is block device"
	exit 1
fi



### Check size of $DEV and create same-size block device for $SNAPSHOT
if test -b $DEV ; then
        DEV_SIZE=$(parted $DEV -s 'unit s' -s ' print' | \
                grep "Disk $DEV" | grep -o '[0-9]\+s' | grep -o '[0-9]\+')
        dd if=/dev/zero of=$SNAPSHOT count=$DEV_SIZE
else
        echo "$DEV is not block device."
        exit 1
fi



### Create loopback device for $SNAPSHOT
SNAPSHOT_LOOPDEV=$(losetup -f --show $SNAPSHOT)



### Dump while skipping bad sector
SKIP=0
IO_ERR=1
ERROR_MSG=
while [ $IO_ERR == 1 ]; do
	ERROR_MSG=`dd if=$DEV of=${SNAPSHOT_LOOPDEV} skip=$SKIP seek=$SKIP 2>&1`
	IO_ERR=$?
	COPIED_SECTORS=$(( `echo $ERROR_MSG | grep -o  'out [0-9]\+ bytes' | grep  -o '[0-9]\+'` / 512 ))
	if [ $COPIED_SECTORS == 0 ] ; then
		SKIP=$(( SKIP + 1 ))
		echo "$SKIP: bad sector"
	else
		SKIP=$(( SKIP + COPIED_SECTORS ))
	fi
done


### Cleanup
losetup -d $SNAPSHOT_LOOPDEV


