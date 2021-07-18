### docker-pull
Pull image from path of compressed archives
```
.function/docker-pull  fedora:32.tar.gz
```



### docker-build
```
.function/docker-build  ${PATH_TO_DOCKERFILE}
```

### symlink
Make a symlink in search path
```
sudo .function/symlink  ${PATH_TO_FILE}
```


### runbox
Run interactive bash:
```
DKR_IMAGE=fedora:32.6084fb96    .function/runbox bash
```

Run a ```.function``` script: (```runbox``` must be ```symlink``` beforehand)
```
cd ${CONTEXT_DIR}
DKR_IMAGE=fedora:32.6084fb96    runbox .function/${FUNCTION_NAME}  ${ARGS}
```

Run with port published:
```
cd ${CONTEXT_DIR}
DKR_EXPOSE=443    runbox .function/${FUNCTION_NAME} ${ARGS}
```
