This page present usage of print command that may be useful in kube debugging

### Print pod
List all containers inside pod with its port and invoke command
```
POD_NAME=
NAMESPACE=
bash> ./print-pod.sh  ${POD_NAME} ${NAMESPACE}
```

### Print service backends
List all pods associated with service endpoint by labels
```
SERVICE_NAME=
NAMESPACE=
bash> ./print-svc-backends.py  ${SERVICE_NAME} ${NAMESPACE}
```
