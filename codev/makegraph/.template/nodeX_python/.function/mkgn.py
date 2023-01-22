
import sys
import os
sys.path.append('./modules/')

import yaml
import json
import time
import hashlib
import os.path
# ----
def yson(namespace,dict_block,file):
    print(namespace + ": " + json.dumps(dict_block), file=file)

def stdlog(event):
    epoch_time = str(int(time.time()))
    if not event is dict: event = '{"event":"' + str(event) + '"}'
    print(epoch_time + ": " + str(event))

def md5sum(md5_file,mkgn_input):
    MD5SUM = open(md5_file,'w')
    for node in mkgn_input.keys():
        hlib.update(bytes(INPUT[node],'utf-8'))
        md5_code = str(hlib.digest())
        md5_dict = { "md5": md5_code }
        yson(node,md5_dict,MD5SUM)

def md5check(md5_file,mkgn_input):
    MD5SUM = open(md5_file,'w')
    for node in mkgn_input.keys():
        hlib.update(bytes(INPUT[node],'utf-8'))
        md5_code = str(hlib.digest())
        md5_dict = { "md5": md5_code }
        yson(node,md5_dict,MD5SUM)

# ----
NAME = os.environ['MKGN_NAME']
OUTPUT = open(os.environ['MKGN_OUTPUT'],'w')
INPUT = {}
list_subnode = os.environ['MKGN_INPUT'].split(':')[1].split()

for node in list_subnode:
    file = f'{os.environ["GRAPH_DIR"]}/{node}'
    with open(file,'r') as f:
        INPUT[node] = f.read()


MD5SUM = ".tmp/input.md5sum"
hlib = hashlib.md5()
if not os.path.exists(".tmp") : os.mkdir(".tmp")
if os.stat(os.environ['MKGN_OUTPUT']).st_size == 0 : os.remove(MD5SUM)
if len(list_subnode) != 0:
    print("subnode")
    if not os.path.exists(MD5SUM):
        MD5SUM = open(MD5SUM,'w')
        print(MD5SUM)
        # 1st compute
        for node in INPUT.keys():
            hlib.update(bytes(INPUT[node],'utf-8'))
            md5_code = str(hlib.digest())
            md5_dict = { "md5": md5_code }
            yson(node,md5_dict,MD5SUM)
    elif: md5sum -c $MD5SUM ; then
        print("Input not changed. Skipped")
        exit(0)
#    else
#        # recompute
#        md5sum  ${MKGN_INPUT[@]} > $MD5SUM
#    fi
#fi


