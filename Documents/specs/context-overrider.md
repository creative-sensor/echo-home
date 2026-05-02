# DEFINITION

`Context-Overrider` is a top-level shell script or command or program which allow `Overriding Behavior through Local Discovery`

- the context is defined as $(pwd) where user invoke the top-level script or command
- if $(pwd)/.function/$0 is found an executable script then allow to override itself via bash builtin `exec` or other methods
- the top-level script must be exported globally in PATH to be invokable everywhere

# INSTANCES


```bash
#!/bin/bash

# CHECK TO OVERRIDE
test -x .function/$0 && exec .function/$0

# OVERRIDE IS SKIPPED
echo "This is the main part of the script where everything goes normal without overriden"
```
