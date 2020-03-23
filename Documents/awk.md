### search ,
```sh
awk '/dnf/' ~/.bash_history
```

### run-code-from-file ,
```sh
awk -f $file
```

### template-code ,
```awk
@include "bingo-title.awk"

BEGIN {
    for (i = 1; i < = 5; i++) {
        b = int(rand() * 15) + (15*(i-1))
        printf "%s\t", b
    }
    print
}
```


### search-line-and-print-field ,
```bash
keyword=
field_delimiter=
file=
awk -F "$field_delimiter" '/$keyword/{ print $2 }'   $file
```

### search-and-replace-multiline-pattern ,
```bash
file=
awk '/PATTERN_START/,/PATTERN_END/' $file
```
