#!/bin/bash
MOUTPOINT=$1
echo $MOUTPOINT | grep -o '[a-fA-F0-9]\+$'
