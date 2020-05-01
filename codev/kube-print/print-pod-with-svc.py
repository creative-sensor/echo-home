#!/usr/bin/env python
import sys , subprocess, json

POD_NAME = sys.argv[1]
NAMESPACE = sys.argv[2]

if NAMESPACE is None: NAMESPACE = "default"

# SVC CONVERSION
svc_list = subprocess.check_output("./print-svc-list.sh " + NAMESPACE, shell=True)
svc_list = svc_list.decode('utf-8').split(' ')


# POD LABELS CONVERSION
pod_labels = subprocess.check_output("./print-pod-with-label.sh " + POD_NAME + " " + NAMESPACE, shell=True)
pod_labels = pod_labels.decode('utf-8')
pod_labels = json.loads(pod_labels)


# FIND MATCHING
match_list = []
for svc in svc_list:
    svc_labels = subprocess.check_output("./print-svc-selectors.sh " + svc + " " + NAMESPACE, shell=True)
    svc_labels = svc_labels.decode('utf-8')
    try:
        svc_labels = json.loads(svc_labels)
    except Exception as e:
        print("ERROR: cannot load svc labels")
        continue
    for key in pod_labels:
        try:
            if svc_labels == None: continue
            if svc_labels[key] == pod_labels[key]:
                match_list.append(svc)
                break
        except KeyError:
            continue
print(match_list)
            
