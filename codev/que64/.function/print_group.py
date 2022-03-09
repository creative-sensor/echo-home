#!/usr/bin/python3
import sys
import pprint
sys.path.append("./")
import VARSET

GROUP = {}
for key in sorted(VARSET.que64.keys()):
    name = VARSET.que64[key]["name"] 
    state = key
    group = name.split(' ')[0]
    group = group.upper()
    try:
        GROUP[group][state] = name
    except KeyError:
        GROUP[group] = {}
        GROUP[group][state] = name
    

for group in GROUP.keys():
    print("----" * 12)
    for state in sorted(GROUP[group]):
        name = VARSET.que64[state]["name"]
        index = int(state,2)
        print(f"{group:<6} | {name:<24} | {state} | {index}")

