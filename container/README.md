### docker-pull
Pull image name from compressed archives in ```.embedded``` directory
```
.function/.embedded/
.function/.embedded/fedora:32.tar.gz
.function/.embedded/ubuntu:20.04.tar.gz
.function/.embedded/fedora:32.sens.608500a7.tar.gz
.function/.embedded/fedora:32.nothing.60850069.tar.gz

.function/docker-pull  fedora:32.sens.608500a7
```



### docker-build
```
DOCKERFILE=Dockerfile.${NAME}    .function/docker-build  ${SOURCE_DIR} ${SUBTAG}
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
