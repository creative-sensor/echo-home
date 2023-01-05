### MODEL

```
NAMESPACE_A : {"Name":"yson","varNum":"666","varStr":"ooooooooooo"}
NAMESPACE_BB : {"Name":"yaml","scope":"space","varX":10}
NAMESPACE_CCC : {"Name":"json","scope":"block"}

NAMESPACE_CC : {"varA":"44444","scope":"added"}
NAMESPACE_VXX : {"varHi":"800"}
```


### FUNCTION
- Read key
```bash
cat spec.yson  |  .function/start NAMESPACE_BB.scope
.function/start NAMESPACE_BB.scope  spec.yson
```

- Set key to stdout or replace in file
```bash
cat spec.yson  |  .function/start NAMESPACE_BB.scope=wide

.function/start NAMESPACE_BB.scope=wide  spec.yson
```

- Insert new key and namespace
```bash
cat spec.yson  |  .function/start NAMESPACE_NEW.varT=gggggg

.function/start NAMESPACE_NEW.varT=hhhhh spec.yson

```


