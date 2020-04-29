#!/bin/bash
SVC_NAME=$1


kubectl  get svc "${SVC_NAME}" -o=json | \
    jq  ' { "clusterIP" : .spec.clusterIP , "type": .spec.type}'

kubectl get svc ${SVC_NAME} -o json  |  jq   '.spec.ports[] | {"nodePort": .nodePort,"port": .port}'

kubectl  get node -o json | jq '.items[].status.addresses[0].address'
