# vim : rc/artillery
--------------------------------
### 0 DESCRIPTION
- to support file edit in a remote system

### 1 SOLUTION


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

###### 4.0
prototype
```
vim-artillery host-abc
    terminal:ssh host-acb
    window: list
        ssh host-abc:file > file buffer
        file save
        ssh > host-abc:file
```

--------------------------------
```json
{ "project_code": "LEAF" , "links": "" , "location": "" , "fpoint": "" }
```
