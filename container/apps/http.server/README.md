### IMAGES
```
sudo docker images  --no-trunc --digests python:3.9.10
REPOSITORY   TAG       DIGEST    IMAGE ID                                                                  CREATED      SIZE
python       3.9.10    <none>    sha256:f88f0508dc467046a760e8ea0bb5c5861ff4e0ade96226ca4700abd1bd28b696   7 days ago   912MB

```


### RUNBOX
```
DKR_IMAGE=python:3.9.10 DKR_EXPOSE=8182:8182   runbox .function/start
```
