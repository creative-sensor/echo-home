#!/usr/bin/python3

import sys
import base64
import pprint
import jwt
import os.path
import json
import time
import pprint


sys.path.append("./")
import VARSET


FILENAME = sys.argv[1]
PAYLOAD = None

with open(FILENAME,'r') as f:
    data = f.read()
    try:
        PAYLOAD = json.loads(data)
    except Exception as e:
        PAYLOAD = { "visa": data }

PAYLOAD["iat"] = time.time()
PAYLOAD["exp"] = time.time() + VARSET.duration
encoded = jwt.encode(PAYLOAD, VARSET.key, VARSET.algorithms)


FILENAME="token-" + hex(int(PAYLOAD["exp"]))
with open(FILENAME,'w') as f:
    f.write(encoded)
    pprint.pprint({"id": FILENAME , "token" : encoded})
