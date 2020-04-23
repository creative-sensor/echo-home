#!/bin/bash -x

PREFIX=$1

helm uninstall ${PREFIX}

for i in 0 1 2 ; do
    kubectl delete pvc datadir-mgdb-mongodb-replicaset-${i}
done

for i in 0 1 2 ; do
    kubectl delete pv mgdb-volume-${i}
done
