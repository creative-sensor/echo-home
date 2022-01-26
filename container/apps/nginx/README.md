### IMAGES
```
sudo docker images  --no-trunc --digests nginx:1.21.4
REPOSITORY   TAG       DIGEST    IMAGE ID                                                                  CREATED       SIZE
nginx        1.21.4    <none>    sha256:f6987c8d6ed59543e9f34327c23e12141c9bad1916421278d720047ccc8e1bee   5 weeks ago   141MB
```

### RUNBOX
```
DKR_IMAGE=nginx:1.21.4 DKR_EXPOSE=443:443    runbox .function/start
```
