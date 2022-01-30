### IMAGES
```
sudo docker images  --no-trunc --digests elasticsearch:7.16.3
REPOSITORY      TAG       DIGEST    IMAGE ID                                                                  CREATED       SIZE
elasticsearch   7.16.3    <none>    sha256:3a5e932847815e81e74ca6179f584f2af458867bfe5dd6fad2ebc2dab32a2121   3 weeks ago   611MB
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=elasticsearch:7.16.3   DKR_EXPOSE=9200:9200  runbox .function/start 
```
