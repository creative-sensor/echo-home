# VIM : remove aged Session.vim
--------------------------------
### 0 DESCRIPTION
Keeping Session.vim for too long make lots of unused buffers re-opened hence downgrade performance

### 1 SOLUTION
https://github.com/creative-sensor/echo-home/commit/14897793dcbc6df7b7747f3cffe04829b974c466

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

###### 4.0

As Session.vim is frequently modified, we can't apply the command below
```vim
exec "! set -x ; rm $(find Session.vim -mtime +7)"
```



--------------------------------
```json
{ "project_code": "LEAF" , "links": "" , "location": "" , "fpoint": "" }
```
