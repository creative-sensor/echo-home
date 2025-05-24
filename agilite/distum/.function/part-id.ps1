#!/bin/bash

MOUTPOINT=$1
echo $MOUTPOINT | awk -F"-" '{print $NF}'
