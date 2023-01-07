#!/usr/bin/env python

# ---- VARSET:python 
import sys
try:
    KEYPATH = sys.argv[1]
    SOURCE_FILE = open(sys.argv[2],'r')
except IndexError as msg:
    if not 'KEYPATH' in locals() : KEYPATH = "print.nothing"
    if not 'SOURCE_FILE' in locals() : SOURCE_FILE = sys.stdin 
# ----



# ---- MAIN ----
import yaml
import json
import pprint

yson = yaml.safe_load(SOURCE_FILE)
write = False
if '=' in KEYPATH:
    KEYPATH, VALUE = KEYPATH.split('=')
    write = True

if '.' in KEYPATH:
    NAMESPACE, KEY = KEYPATH.split('.')


if write :
    if '.' in KEYPATH:
        try:
            yson[NAMESPACE][KEY] = VALUE
        except KeyError:
            yson[NAMESPACE]= {}
            yson[NAMESPACE][KEY] = VALUE
    else:
        yson[KEYPATH] = VALUE

    if not SOURCE_FILE is sys.stdin: 
        SOURCE_FILE_name = SOURCE_FILE.name
        SOURCE_FILE.close()
        with open(SOURCE_FILE_name,'w') as SOURCE_FILE:
            for name in yson.keys():
                json_str = json.dumps(yson[name])
                print(f'{name} : {json_str}', file = SOURCE_FILE)

    else:
        for name in yson.keys():
            json_str = json.dumps(yson[name])
            print(f'{name} : {json_str}')
else:
    try:
        if '.' in KEYPATH:
            print(yson[NAMESPACE][KEY])
        else:
            print(json.dumps(yson[KEYPATH]))
    except:
        print("")



