### IMAGES
```
sudo docker images  --no-trunc --digests bitnami/prometheus:2.32.1
REPOSITORY           TAG       DIGEST    IMAGE ID                                                                  CREATED        SIZE
bitnami/prometheus   2.32.1    <none>    sha256:f3b18775c4fe81d3adcee2d3801984f21d593839a3c9c041445dc9481faa9ee1   18 hours ago   283MB
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=bitnami/prometheus:2.32.1 DKR_EXPOSE=9090:9090    runbox .function/start
```
