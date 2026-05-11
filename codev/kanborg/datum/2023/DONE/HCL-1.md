# FEATURE : Consul as dns resolver
--------------------------------
### 0 DESCRIPTION
Configure consul as dns resolver for workstation

### 1 SOLUTION
- start graph ```uplink``` (implemented in MKG-2)
- network manager to set DNS static: ```127.0.0.1,8.8.8.8```


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 DISCUSSION

###### 4.0
- https://developer.hashicorp.com/consul/tutorials/networking/dns-forwarding


--------------------------------
```json
{ "project_code": "HCL" , "links": "MKG-2" , "location": "" , "fpoint": "" }
```
