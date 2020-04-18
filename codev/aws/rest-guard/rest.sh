#!/bin/bash -xe

# shutdown when there is no active network connection
if  ! ss -tupn | grep 'ESTAB.*:443.*:[0-9]\+  '  ; then
    shutdown -h 0
fi
