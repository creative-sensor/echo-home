#!/bin/bash
source $(git_root)/codev/commonwealth/base
source $CMWBASE/vault vault

dict vault.token=100000
dict gitlab.repo.t=dkl
dict gitlab.repo.x=33
dict gitlab.repo.size=10
dict gitlab.user.name=bao

echo "Attempt to set sub-keypath: must be failed"
dict gitlab.repo.size.m=8

echo " ---- List g*"
dict g*

echo " ---- List gitlab.rep*"
dict gitlab.rep*
