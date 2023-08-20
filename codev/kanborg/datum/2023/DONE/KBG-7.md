# file VIEW : unable to open in Windows Terminal
--------------------------------
### 0 DESCRIPTION

- Windows terminal shell config: https://github.com/creative-sensor/echo-home/blob/5615115cb2a9d81affa3ee97b1c96a0bf5369a00/windows/.terminal.settings.json#L79

- Under Windows Terminal, open temp file and hit error: https://github.com/creative-sensor/echo-home/blob/5615115cb2a9d81affa3ee97b1c96a0bf5369a00/codev/kanborg/.vimrc#L80

```
Error detected while processing VimEnter Autocommands for "*"..function Kolum

line    4:
E485: Can't read file C:/Users/${USER}/AppData/Local/Temp/VNJ68ED.tmp
```


### 1 SOLUTION

- https://github.com/creative-sensor/echo-home/commit/8fce17b33e0574db6cbe74d2df25ccc18526ed4f

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL



--------------------------------
```json
{ "project_code": "KBG" , "links": "" , "location": "" , "fpoint": "" }```
