# DEFINITION

`Context-Overrider` is a top-level shell script or command or program which allow `Overriding Behavior through Local Discovery`

- $(pwd) is the context
- if .function/$0 is an executable script then allow to override itself via bash builtin `exec` or other methods
- the script must be exported globally in PATH to be invokable everywhere

# INSTANCES

See `demo.sh`:

```bash
#!/bin/bash

# ---- SETUP OVERRIDER ----
mkdir -p .function
cat > .function/$0 <<EOF
#!/bin/bash
echo "but context:overriden by .function/$0"
echo "UPPERCASED"
EOF
chmod 0755 .function/$0
# ---- END OF SETUP ----

echo "lowercased typically"

test -x .function/$0 && exec .function/$0
```
