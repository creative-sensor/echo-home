#!/usr/bin/python3
import sys
sys.path.append("./")
import VARSET

for key in sorted(VARSET.que64.keys()):
    INDEX = int(key,2) 
    STATE = key
    NAME = VARSET.que64[key]["name"]
    print(f"{INDEX:<3} | {STATE}  {NAME}")
