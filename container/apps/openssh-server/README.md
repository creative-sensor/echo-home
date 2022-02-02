### IMAGES
```
sudo docker images  --no-trunc --digests fedora:34
REPOSITORY   TAG       DIGEST    IMAGE ID                                                                  CREATED        SIZE
fedora       34        <none>    sha256:e1f83fe2ddbd248653cf952374c351ffbef387339f746983e9d32522ec005ed7   2 months ago   178MB
```

### HOST/GUESTS
```
.function/host-new

.function/user-new ${USER_NAME}

```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=fedora:34 DKR_EXPOSE=2022:2022 runbox .function/start
```
