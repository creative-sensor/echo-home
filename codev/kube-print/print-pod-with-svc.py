#!/usr/bin/env python
import sys , subprocess

POD_NAME = sys.argv[1]

svc_list = subprocess.check_output("./print-svc-list.sh", shell=True)
svc_list = svc_list.decode('utf-8').split(' ')

match_list = []
for svc in svc_list:
    if POD_NAME.find(svc) != -1:
        match_list.append(svc)

print(match_list)
            
