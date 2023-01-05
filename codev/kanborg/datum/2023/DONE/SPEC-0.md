# SPEC : yson format
--------------------------------
### 0 DESCRIPTION

```json
NAMESPACE_A : {"Name":"yson","varNum":"666","varStr":"ooooooooooo"}
NAMESPACE_BB : {"Name":"yaml","scope":"space","varX":10}
NAMESPACE_CCC : {"Name":"json","scope":"block"}
NAMESPACE_CC : {"varA":"44444","scope":"added"}
NAMESPACE_VXX : {"varHi":"800"}
```

### 1 SOLUTION

echo-home/commit/```219cf42e722b0c0ac66d6e45154dbe80fcc9719b```

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 DISCUSSION
###### 4.2
https://github.com/creative-sensor/echo-home/commit/219cf42e722b0c0ac66d6e45154dbe80fcc9719b

###### 4.1
to support reading from stdin and write to stdout when using with pipe

###### 4.0
key read/write

https://github.com/creative-sensor/echo-home/commit/24cec87860345c21f8afb4976f2ced08bfb4ed5a


--------------------------------
```json
{ "project_code": "SPEC" , "links": "yson" , "location": "code/yson" , "fpoint": "1" }
```
