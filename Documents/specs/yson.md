# DEFINITION

- YSON is a hybrid format of half-yaml and half-json
- the yaml part indicates entries, each of which takes 1 line in the whole yson structure 
- the json part must be inline block (the body of an entry) and can have any sub-keys predefined by user following json standards

# STRUCTURE

```
ENTRY_NAME: { "<SUB_KEY1>":"<VALUE1>" , "<SUB_KEY2>":"VALUE2" , "SUB_KEYX":"VALUEX" }
```

# INSTANCES

```
20260102: {"event":"write","size":"10GB"}
20260124: {"event":"read","size":"40GB","error":"io"}
20260411: {"event":"power-off","reason":{"name":"power-loss","method":"unplug"}}
```
