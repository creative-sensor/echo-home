### IMAGES
```
sudo docker images  --no-trunc --digests redis:6.2.6
REPOSITORY   TAG       DIGEST    IMAGE ID                                                                  CREATED       SIZE
redis        6.2.6     <none>    sha256:7614ae9453d1d87e740a2056257a6de7135c84037c367e1fffa92ae922784631   5 weeks ago   113MB
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=redis:6.2.6 DKR_EXPOSE=6379:6379    runbox .function/start
```
