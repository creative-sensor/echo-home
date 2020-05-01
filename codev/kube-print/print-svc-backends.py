#!/usr/bin/env python
import os , sys , json , subprocess

SVC_NAME = sys.argv[1]
NAMESPACE = "default"
if len(sys.argv) == 3: NAMESPACE = sys.argv[2]

class bcolors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

selector = subprocess.check_output("./print-svc-selectors.sh " + SVC_NAME + " " + NAMESPACE, shell=True)
selector = json.loads(selector)
for key in selector:
    cmd = "./print-svc-backends-with-label.sh " + SVC_NAME + " " + key + " " + selector[key] + " " + NAMESPACE
    output = subprocess.check_output(cmd, shell=True)
    print(bcolors.BOLD + "LABEL: " + key + " = " + selector[key] + bcolors.ENDC)
    print(output.decode("utf-8"))

