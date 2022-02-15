
### FUNCTIONS
```
- View status:
    .function/status
- Add user:
    .function/user $NAME
- Kill samba server:
    .function/kill 
```


### NOBOX
```
# run samba server as $USER:
    sudo .function/start $USER
```


### RUNBOX
```
DKR_IMAGE=fedora:34 DKR_EXPOSE=1390:1390 runbox .function/start
```

### PORT FORWARDING
Since Windows Explorer requires exact port 445/139 so port must be forwarded:
```

```
