#!/usr/bin/env python



#-------------------------||| IPv6 REGEX PANTHEON |||---------------------------
FULL = '([A-Fa-f0-9]{1,4}):(([A-Fa-f0-9]{1,4}):){6}([A-Fa-f0-9]{1,4})'
MIDDLE_ZERO_61 = '(([A-Fa-f0-9]{1,4}:){6}(:[A-Fa-f0-9]{1,4}){1})'
MIDDLE_ZERO_52 = '(([A-Fa-f0-9]{1,4}:){5}(:[A-Fa-f0-9]{1,4}){1,2})'
MIDDLE_ZERO_43 = '(([A-Fa-f0-9]{1,4}:){4}(:[A-Fa-f0-9]{1,4}){1,3})'
MIDDLE_ZERO_34 = '(([A-Fa-f0-9]{1,4}:){3}(:[A-Fa-f0-9]{1,4}){1,4})'
MIDDLE_ZERO_25 = '(([A-Fa-f0-9]{1,4}:){2}(:[A-Fa-f0-9]{1,4}){1,5})'
MIDDLE_ZERO_16 = '([A-Fa-f0-9]{1,4}:(:[A-Fa-f0-9]{1,4}){1,6})'
TAIL_ZERO = '(([A-Fa-f0-9]{1,4}:){1,6}:)'
LEADING_ZERO = '(:(:[A-Fa-f0-9]{1,4}){1,6})'
ALL_ZERO = '(::)'

LONGEST_MATCH_PRIORITY = [ 
            FULL,
            MIDDLE_ZERO_61,
            MIDDLE_ZERO_52,
            MIDDLE_ZERO_43,
            MIDDLE_ZERO_34,
            MIDDLE_ZERO_25,
            MIDDLE_ZERO_16,
            TAIL_ZERO,
            LEADING_ZERO,
            ALL_ZERO
          ]

PATTERN = "|".join(LONGEST_MATCH_PRIORITY)
#-------------------------------------------------------------------------------





import re

#print("PATTERN:   " + PATTERN)
regex = re.compile(PATTERN)
with open("./test-cases.ipv6",'r') as f:
    for line in f:
        if len(line) > 1 : prefix = line.split()[0]
        #result = regex.findall(line)
        result = regex.search(line)
        #if result : print(prefix + "   " + str(result))
        if result : print(prefix + "   " + result.group())

