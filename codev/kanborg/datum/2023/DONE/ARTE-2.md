# ISSUE : luks failed to active with passphrase
--------------------------------
### 0 DESCRIPTION


### 1 SOLUTION

- Caused by typo (a lowercase was not expected)

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

###### 4.0
passphrase has been changed:
```
sudo cryptsetup luksChangeKey /dev/sdb -S 0
```

--------------------------------
```json
{ "project_code": "ARTE" , "links": "" , "location": "" , "fpoint": "" }
```
