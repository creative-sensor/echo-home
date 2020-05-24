### Run registry server
```
docker run -d -p 5000:5000 --restart=always --name registry registry:2
```


### Tag image with address of remote registry server
```
docker tag   IMG_NAME:LOCAL_TAG        REGISTRY_ADDR:5000/IMG_NAME:REMOTE_TAG
```


### Configure docker client connect to registry by insecure
```
cat > /etc/docker/daemon.json 

{
  "insecure-registries" : ["REGISTRY_ADDR:5000"]
}
```


### Push image to registry
```
docker push  REGISTRY_ADDR:5000/IMG_NAME
```


### List all images
```
curl -X GET http://REGISTRY_ADDR:5000/v2/_catalog
```

### List all tags of an image
```
curl -X GET http://REGISTRY_ADDR:5000/v2/IMG_NAME/tags/list
```
