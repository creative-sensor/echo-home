### IMAGES
```
sudo docker images  --no-trunc --digests mariadb:10.5.12
REPOSITORY   TAG       DIGEST    IMAGE ID                                                                  CREATED        SIZE
mariadb      10.5.12   <none>    sha256:9a2e250ac49189e065785b9744b87ec79472c9fa20766aafd25af62a40953316   3 months ago   407MB
```
### CREATE A NEW DB
```
.function/mysql-db-new
```

### CONSOLE FOR ROOT ACCESS
```
.function/console
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=mariadb:10.5.12 DKR_EXPOSE=3306:3306    runbox .function/start
```
