#!/usr/bin/env python

import sys
import json

FILENAME = sys.argv[1]

### READ DATA
with open(FILENAME,'r') as f:
    print("Load from file: " + str(json.load(f)))
    print("-------------------------------------")

with open(FILENAME,'r') as f:
    str_data = f.read()
    print("Load from str (.loads): " + str(json.loads(str_data)))
    print("-------------------------------------")

### JSON PATH
with open(FILENAME,'r') as f:
    data = json.load(f)
    print("Value of JSON path:")
    print(".items[0].metadata.name = " + data["items"][0]["metadata"]["name"])
    print("-------------------------------------")

    print("Set new value: 10.230.0.1")
    data["items"][0]["metadata"]["name"] = "10.230.0.1"
    print(".items[0].metadata.name = " + data["items"][0]["metadata"]["name"])
    print("-------------------------------------")


with open(FILENAME+".oneline" , 'w') as f:
    print("Dump json data to file");   json.dump(data,f)
    print("-------------------------------------")

with open(FILENAME+".pretty" , 'w') as f:
    print("Dump pretty json data to file");   json.dump(data , f , sort_keys=False , indent=4)
