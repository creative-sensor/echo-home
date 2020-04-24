### List cluster
```
aws eks --profile $PROFILE_NAME  list-clusters
```

### List context
```
kubectl config get-contexts
```

### Add cluster config to kubectl client
```
aws eks --profile $PROFILE_NAME  --region $REGION  update-kubeconfig --name $CLUSTER_NAME
```

### Switch between clusters/context in .kube/config
```
CONTEXT_NAME=
kubectl config use-context $CONTEXT_NAME
```

### List namespaces under current context
```
kubectl get namespace
```

### List pods under namespace 
```
NAMESPACE=
kubectl -n $NAMESPACE get pods
```

### Describe pods
```
NAMESPACE=
kubectl -n $NAMESPACE describe pods
```

### Describe service
```
NAMESPACE=
SERVICE_NAME=
kubectl -n $NAMESPACE  describe service $SERVICE_NAME
```

### Create namespace
```
kubectl create namespace <insert-namespace-name-here>
```
OR using yaml file:
```
apiVersion: v1
kind: Namespace
metadata:
  name: <insert-namespace-name-here>
```
```
kubectl create -f ./my-namespace.yaml
```


### Display entire process tree containing a particular PID
```
PID=
pstree -p -s  $PID
```


### Enter process's namespace to see how it see environment inside container
```
CMD=
    #command to run inside namespace
PID=
    #pid of process running inside container

sudo nsenter  -t  $PID  -n $CMD
```
