# FEATURE : default implementation for common functions
--------------------------------
### 0 DESCRIPTION
- .function/stop is missing
- generic call: .function.stop --> $CMW/.function.stop

### 1 SOLUTION
https://github.com/creative-sensor/echo-home/commit/0761ee5ebc5d9ba6ed580d772fb9740367572d8f

- Calling function through interface. If function is missing in current objectory, default will be called instead
```bash
ffunction artefacts
```

- Calling function directly
```bash
.function/artefacts
```

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL



--------------------------------
```json
{ "project_code": "CMW" , "links": "" , "location": "" , "fpoint": "" }
```
