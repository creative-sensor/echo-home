### IMAGES
```
sudo docker images  --no-trunc --digests elastic/filebeat:7.16.3
REPOSITORY         TAG       DIGEST    IMAGE ID                                                                  CREATED       SIZE
elastic/filebeat   7.16.3    <none>    sha256:5981b1211cbf9aa1100287278f99e3bc74fa383a72d849dffa7c1c1ce8a0c345   3 weeks ago   508MB
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=elastic/filebeat:7.16.3    runbox .function/start
```
