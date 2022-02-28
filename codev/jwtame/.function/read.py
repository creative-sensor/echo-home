#!/usr/bin/python3
import sys
import base64
import pprint
import jwt
import os.path

sys.path.append("./")
import VARSET


FILENAME = sys.argv[1]
TOKEN = None
with open(FILENAME,'r') as f:
   TOKEN = f.readline().rstrip("\n")


print(jwt.decode(TOKEN, VARSET.key, VARSET.algorithms))
