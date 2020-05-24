### delete-lines-between-2-pattterns ,
```
file=
sed  '/PATTTERN_START/,/PATTERN_END/d'    $file
```

### case-conversion
```
FILE=
sed 's/PATTERN/\U&/g'   ${FILE}
    #to upper-case
 	
sed 's/PATTERN/\L&/g'   ${FILE}
    #to lower-case
```


### delete-line-number
```bash
LINE_NUMBER=
FILE=
sed ${LINE_NUMBER}d  ${FILE}
```

### delete-line-by-regex , 
```
FILE=
sed '/REGEX/d' ${FILE} 
```

### delete-line-by-range ,
```bash
FILE=
LINE_START=
LINE_END=
sed "${LINE_START},${LINE_END}d" ${FILE}
```
