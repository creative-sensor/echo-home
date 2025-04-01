# VERSIONING:  multi-location datum
--------------------------------
### 0 DESCRIPTION
given  the same objectory in different location, how to merge these separate  versions of datum

### 1 SOLUTION

- too much difficult to achieve
- will try this with ```pedrive```

### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

##### 4.2
- stateless write: discrete data insertion (inspired by binlog) but do not care of previous state
```
- "data_ABC" insertion: recorded as file 2023-02-06.ABC
- "data_OPQ" insertion: recorded as file 2023-02-06.OPQ

- data merge: copy 2023-02-06.* into main data dir
```

##### 4.1
- Data versioning with rsync: https://russt.me/2018/07/creating-and-applying-diffs-with-rsync/

##### 4.0
- Redis is key/value DB which probably support merging of multi-version data seamlessly



--------------------------------
```json
{ "project_code": "LEAF" , "links": "" , "location": "" , "fpoint": "" }
```
