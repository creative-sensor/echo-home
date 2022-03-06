### IMAGES
```
sudo docker images  --no-trunc --digests bitnami/rabbitmq:3.9.13
REPOSITORY         TAG       DIGEST    IMAGE ID                                                                  CREATED        SIZE
bitnami/rabbitmq   3.9.13    <none>    sha256:689986515e9bc82f43beababd7980ac76d1a360cbca5e8ee26ca34b594f3e4b5   25 hours ago   230MB

```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=bitnami/rabbitmq:3.9.13 DKR_EXPOSE=5672:5672    runbox .function/start
```
