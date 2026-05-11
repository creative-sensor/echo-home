# ISSUE : card not move when destination is empty
--------------------------------
### 0 DESCRIPTION
- scenario
```
HOLD   | WIP
card-a |
card-b |
card-d |
```
moving any car to WIP won't work

- workaround
```
touch datum/WIP/test and reload board
```



### 1 SOLUTION
seems the buffer of empty column was never open

the issue can be fixed by inserting header line of column name to make it non empty

- cherry-pick: ```2c79070cb05cbd7cd2a0f0d8254d18fbd36f97f4```, ignoring README and media. In future we don't pick those changes that touch data

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 DISCUSSION



--------------------------------
```json
{ "project_code": "KBG" , "links": "" , "location": "code/kanborg" , "fpoint": "1" }
```
