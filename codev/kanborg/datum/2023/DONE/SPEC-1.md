# SPEC : yson format - python implement

--------------------------------

### 0 DESCRIPTION

Use python to handle yson to see whether code is more maintainable and improved in performance

### 1 SOLUTION

echo-home/commit/```8b504d1337a5b36101c7593154ff90476f9075af```

### 2 NOTES

### 3 TEST/VERIFICATION

### 4 DISCUSSION

###### 4.0

This is an elegant to parse argument in python with minimal effort:

https://github.com/creative-sensor/echo-home/blob/8b504d1337a5b36101c7593154ff90476f9075af/codev/yson/.function/start.py#L6

```python
import sys
try:
    KEYPATH = sys.argv[1]
    SOURCE_FILE = open(sys.argv[2],'r')
except IndexError as msg:
    if not 'KEYPATH' in locals() : KEYPATH = "print.nothing"
    if not 'SOURCE_FILE' in locals() : SOURCE_FILE = sys.stdin 
```

--------------------------------

```json
{ "project_code": "SPEC" , "links": "SPEC-0 yson" , "location": "codev/yson" , "fpoint": "1" }
```
