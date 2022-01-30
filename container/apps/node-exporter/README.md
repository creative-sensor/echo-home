### IMAGES
```
sudo docker images  --no-trunc --digests bitnami/node-exporter:1.3.1
REPOSITORY              TAG       DIGEST    IMAGE ID                                                                  CREATED        SIZE
bitnami/node-exporter   1.3.1     <none>    sha256:d67bd6f7ac2ba0f7ee31e4be49588f3cfb71a83bea2ad8dea636988dd883718b   25 hours ago   104MB

```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=bitnami/node-exporter:1.3.1 DKR_EXPOSE=9100:9100    runbox .function/start
```
