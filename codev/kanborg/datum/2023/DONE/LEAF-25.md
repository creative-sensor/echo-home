# vim : rc/artillery
--------------------------------
### 0 DESCRIPTION
- to support file edit in a remote system


<img alt="" src="https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/Royal_Artillery_Firing_105mm_Light_Guns_MOD_45155621.jpg/450px-Royal_Artillery_Firing_105mm_Light_Guns_MOD_45155621.jpg"/>

### 1 SOLUTION

https://github.com/creative-sensor/echo-home/commit/b517979f32349fad9ead5c32f70cbdf2e7fb5de6


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
