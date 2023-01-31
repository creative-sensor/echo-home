
import sys
import os
sys.path.append('./modules/')

import yaml
import json
import time
# ----
def yson(namespace,dict_block,file):
    print(namespace + ": " + json.dumps(dict_block), file=file)

def stdlog(event):
    epoch_time = str(int(time.time()))
    if not event is dict: event = '{"event":"' + str(event) + '"}'
    print(epoch_time + ": " + str(event))
# ----
NAME = os.environ['MKGN_NAME']
OUTPUT = open(os.environ['MKGN_OUTPUT'],'w')
INPUT = {}
list_subnode = os.environ['MKGN_INPUT'].split(':')[1].split()

for node in list_subnode:
    file = f'{os.environ["GRAPH_DIR"]}/{node}'
    with open(file,'r') as f:
        INPUT[node] = f.read()

