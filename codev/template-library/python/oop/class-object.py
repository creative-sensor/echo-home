#!/usr/bin/env python

# EXTERNAL FUNCTION
def print_service(obj): 
    print("Color: " + str(obj.color))
    print("Shape: " + str(obj.shape))
    print("Text: " + str(obj.text))


# CLASS DEFINITION
class SimpleA: 
    # default constructor 
    def __init__(self, color , shape , text): 
        self.color = color
        self.shape = shape
        self.text = text
  
    def print(self): 
        print("Color: " + str(self.color))
        print("Shape: " + str(self.shape))
        print("Text: " + str(self.text))


# CLASS EMPTY
class SimpleB:
    """ This is almost an empty class """



# CONSTRUCT OBJECT WITH ATTRIBUTES INITIALIZED BY PARAMS
objA = SimpleA(["green","cyan"] , "triangle" , "smile")

print("* OBJECT-A.METHOD() --------------------------")
objA.print()
print("* NAMESPACE.METHOD(OBJECT-A) -----------------")
SimpleA.print(objA)


# CONSTRUCT EMPTY OBJECT AND ADDING ATTRIBUTES
objB = SimpleB()
objB.color = ["blue","dark"]
objB.shape = "square"
objB.text = "newborn"
objB.print = print_service

print("* OBJECT-B.print() ---------------------------")
objB.print(objB)
# objB.print(objB) : we are calling print_service() actually
# objB.print() won't work as name "SimpleB.print" not defined in class (no passing of args)
# SimpleB.print(objB) won't work as "SimpleB.print" not defined in class
