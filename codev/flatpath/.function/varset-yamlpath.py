#!/usr/bin/env python
import os
import os.path
import sys
import yaml
import pprint
import re
import dpath.util
from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True

PATHSET_FILE = sys.argv[1]
PATHSET = {}
os.chdir(os.path.dirname(PATHSET_FILE))

# ---- BUILD PATHSET DICTIONARY ----
with open(PATHSET_FILE,"r") as f:
    for line in f:
        line_split =  re.split(" \| ", line)
        # extract file path
        file_path = line_split[0]
        # extract key : value
        kv_pair = line_split[1]
        kv_pair_split = re.split(" : ",kv_pair)
        key = kv_pair_split[0]
        value = kv_pair_split[1]
        value = value.rstrip()
        value = value.strip('"')
        try:
            PATHSET[file_path][key] = value 
        except KeyError:
            PATHSET[file_path] = {}
#pprint.pprint(PATHSET)


# ---- UPDATE YAML ----
for file_path in PATHSET.keys():
    #print(f"file_path ================ {file_path}")
    with open(file_path,"r") as f:
        data = yaml.load(f)
        for key in PATHSET[file_path].keys():
            #print(f"key ================ {key}")
            SUBPATH = key
            VALUE = PATHSET[file_path][key]
            # convert to full-stop separator
            SUBPATH = re.sub("\[", "." , SUBPATH)
            SUBPATH = re.sub("\]", "" , SUBPATH)
            #print(f"Updating ----------------- {SUBPATH} / {VALUE}")
            dpath.util.set(data, SUBPATH, VALUE,  separator='.')
        with open(file_path + ".edit.tmp", "w") as f2:
            yaml.dump(data, f2)

