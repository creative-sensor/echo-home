### go-to-first-line ,
```
SHIFT h
```


### go-to-line-number-10 ,
```
1 0
SHIFT g
```


### select-retangular-block-of-text
```
CTRL v
ARROW-KEY
```


### copy,cut,paste
```
y y
    #copy whole line
y w
    #copy from current cursor to end of word
d d
    #cut whole line
d w
    #cut from current cursor to end of word
p
    #paste after current line

SHIFT p
    #paste before current line

```


### comment , uncomment
```
v   ARROW-KEY
    #select line

:s/^/#/
    #comment

:s/^#//
    #uncomment
```


### highlight-indent,
```
/^\( \|\t\)\+
   #note: white space counts
```


### split-window , multi-view
```
CTRL w   v
    #side by side
CTRL w   s
    #stacking
```


### case-invert ,
```
v   ARROW-KEY
    #select text
SHIFT `
    #invert case
```


### save , write-disk ,
```
:wq
    #write and update modification time even if no change in buffer
:x
    #write and update modification time only when there is change in buffer
```


### cursor-movement , jump
```
h        j        k        l
LEFT     DOWN     UP       RIGHT


w
    #jump forwards to the start of a word
W   
    #jump forwards to the start of a word (words can contain punctuation)

e
    #jump forwards to the end of a word
E
    #jump forwards to the end of a word (words can contain punctuation)

b
    #jump backwards to the start of a word
B
    #jump backwards to the start of a word (words can contain punctuation)

```


### screen-movement ,
```
CTRL e
    #move screen up
CTRL y
    #move screen down
```


### tab ,
```
CTRL w   SHIFT t
    #move current buffer to new tab
g   t
    #move to next tab
g   SHIFT t
    #move to previous tab
5   g   t
    #move to tab number 5
```


### buffer ,
```
:bn
   #move to next buffer
:bp
   #move to previous buffer
:b6
   #move to buffer number 6
```


### shift-text ,
```
>   >
   #shift right
<   <
   #shift left
```


### macro ,
```
q   a
    #record macro a
q
    #stop recording macro
@   a
    #run macro a
@   @
    #rerun last run macro
```


### open-tab
```
vim -p   A B C
```


### open-split-window
```
vim -O   A B C
```


### execute-command-on-open ,
```
vim -c 'COMMAND' ${FILE}
```

### run-script ,
```
vim -s ${SCRIPT_FILE}   ${FILE}
```

