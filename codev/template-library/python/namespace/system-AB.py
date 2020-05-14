#!/usr/bin/env python

import sys , os , os.path , json , time

### EXTEND PYHON PATH
sys.path.append('./third-party')
from modules import ModuleA , ModuleB
    #from DIR import FILE[.py]
print("PYTHONPATH: " + str(sys.path))


### USE OBJECT IN ANOTHER NAMESPACE
ModuleA.printNumber(3.141592653589793)
ModuleA.printText("Hello")

ModuleB.printCat()
ModuleB.printDog()

objB = ModuleB.SimpleB([95024459 , 13677028 , 724891227938])
print("OBJ-B = " + str(objB))


### MODULE LOCATE
print("---- MODULE LOCATION ----")
print("* ModuleA: " + ModuleA.__file__)
print("* ModuleB: " + ModuleB.__file__)
print("* json:    " + json.__file__)
print("* os:      " + os.__file__)
print("* os.path: " + os.path.__file__)
print("* time:    " + str(time))
print("* sys:     " + str(sys))
