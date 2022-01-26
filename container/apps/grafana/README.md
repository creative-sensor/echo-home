### IMAGES
```
sudo docker images  --no-trunc --digests grafana/grafana:8.3.4
REPOSITORY        TAG       DIGEST    IMAGE ID                                                                  CREATED      SIZE
grafana/grafana   8.3.4     <none>    sha256:4a34578e43745a6411fc9a4a2fb902f5f731208ff64c28ce9bb5fc6ba603d387   8 days ago   274MB
```

### RUNBOX
```
DKR_RUNAS=root DKR_IMAGE=grafana/grafana:8.3.4 DKR_EXPOSE=3000:3000 runbox .function/start
```
