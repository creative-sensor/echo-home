This page present how mongodb cluster run over kube cluster

### 0 Model
![Persistent-volume](./docs/persistent-volume.svg)
### 1 Edit ```values.yaml```
* replicaSetName:   name of replicaset
* adminUser:   admin user to access mongo db cluster
* adminPassword:   password of DB admin
* key:   text string used for authentication among cluster nodes

Also create these folders for persistent volumes on each Kube node:
```
sudo mkdir -p  /data/mgdb/{0,1,2}
sudo chgrp -R microk8s /data/mgdb
sudo chmod -R 770 /data/mgdb
```
### 2 Install mongodb cluster
```
bash> make cluster
```

### 3 Remove mongodb cluster
```
bash> make clean
```
### 4 Testing
```
bash>

POD_NAME_0=mgdb-mongodb-replicaset-0
kubectl exec -it ${POD_NAME_0} /bin/bash
    # Inside pod

    USER=testuser
    PASS=123
    SERVICE_NAME=mgdb-mongodb-replicaset
    POD_NAME_0=mgdb-mongodb-replicaset-0
    POD_NAME_1=mgdb-mongodb-replicaset-1
    POD_NAME_2=mgdb-mongodb-replicaset-2
    RS_NAME=rstest
    
    mongo "mongodb://${USER}:${PASS}@${POD_NAME_0}.${SERVICE_NAME},${POD_NAME_1}.${SERVICE_NAME},${POD_NAME_2}.${SERVICE_NAME}/?authSource=admin&replicaSet=${RS_NAME}"
```

Example:
```
bash>

kubectl exec -it mgdb-mongodb-replicaset-0 /bin/bash

mongo "mongodb://testuser:123@mgdb-mongodb-replicaset-1.mgdb-mongodb-replicaset,mgdb-mongodb-replicaset-2.mgdb-mongodb-replicaset,mgdb-mongodb-replicaset-0.mgdb-mongodb-replicaset/?authSource=admin&replicaSet=rstest"
```
