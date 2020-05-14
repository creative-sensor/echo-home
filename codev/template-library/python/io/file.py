#!/usr/bin/env python

import sys

FILENAME = sys.argv[1]

### READ DATA
with open(FILENAME,'r') as f:
    print("One line: " + f.readline())
    print("Next line: " + f.readline())
    print("Cursor at: " + str(f.tell()))
    print("Rewind cursor: ") ; f.seek(0, 0)
    print("Entire file:" + f.read())


### ITERATE OVER LINES
print("ITERATE:")
with open(FILENAME,"r+") as f:
    for line in f:
        print(line)


### APPEND DATA
with open(FILENAME,'a') as f:
    f.write("APPENDEDDDDDDDDDDDDDD")


### OVERWRITE DATA
with open(FILENAME,'w') as f:
    f.write("OVERWRITTENNNNNNNNNNN")


### READ AND APPEND
with open(FILENAME,"r+") as f:
    print("First 4 chars: " + f.read(4))
    print("Write amid file"); f.write("###")
    print("Cursor at: " + str(f.tell()))
    print("Next 4 chars is empty: " + str(f.read(4)))



