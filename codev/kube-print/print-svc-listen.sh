#!/bin/bash
SVC_NAME=$1
NAMESPACE=$2


test -z ${NAMESPACE} && NAMESPACE=default
kubectl  get svc "${SVC_NAME}" -n ${NAMESPACE} -o=json | \
    jq  ' { "clusterIP" : .spec.clusterIP , "type": .spec.type}'

kubectl get svc ${SVC_NAME} -n ${NAMESPACE} -o json  |  jq   '.spec.ports[] | {"nodePort": .nodePort,"port": .port}'

kubectl  get node -n ${NAMESPACE} -o json | jq '.items[].status.addresses[0].address'
