#!/usr/bin/python3
import sys
import pprint
sys.path.append("./")
import VARSET

LIST_NAME = {}
for key in sorted(VARSET.que64.keys()):
    NAME = VARSET.que64[key]["name"] 
    STATE = key
    LIST_NAME[NAME] = STATE

GROUP = None
for name in sorted(LIST_NAME.keys()):
    group = name.split(' ')[0]
    group = group.upper()
    state = LIST_NAME[name]
    if group != GROUP:
        GROUP = group
        print("----" * 12)
    print(f"{group:<6} | {name:<24} | {state}")
