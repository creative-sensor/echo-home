#!/usr/bin/env python

### This script print dictionary structure of an object as debug tool

from pprint import pprint
import requests

x = requests.get('https://www.google.com')
x__dict__ = vars(x)


### PRETTY PRINT
pprint(x__dict__)

### PRINT FIELD
print("headers.Cache-Control = " + str(x__dict__["headers"]["Cache-Control"]))

