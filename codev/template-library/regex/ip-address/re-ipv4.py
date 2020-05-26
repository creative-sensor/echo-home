#!/usr/bin/env python



#-------------------|| IPv4 REGEX PANTHEON ||---------------------#
OCTET = '(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
PATTERN = OCTET + "\." + OCTET + "\." + OCTET + "\." + OCTET
#-----------------------------------------------------------------#



import re

print("PATTERN:   " + PATTERN)
regex = re.compile(PATTERN)
with open("./test-cases.ipv4",'r') as f:
    for line in f:
        if len(line) > 1 : prefix = line.split()[0]
        result = regex.search(line)
        if result : print(prefix + "    " + result.group())

