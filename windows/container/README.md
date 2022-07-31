### .function/runbox-datum
- Runbox that mount git-root and datum-root for gedrive
```
export USERNAME=root
DKR_IMAGE=fedora:home  .function/runbox-datum
```
- Inside runbox:
```
cd /home/root/agilite/gedrive
.function/mkfs.gedrive
```
