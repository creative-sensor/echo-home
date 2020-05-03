#!/bin/bash -xe

PREFIX=$1

node=$(kubectl  get node -o json | jq '.items[0].status.addresses[0].address')
node=${node//\"/}

if test -z ${PREFIX} ; then echo PREFIX="" && exit 1 ; fi
if grep --color "^[^#].* ${PREFIX}\..*" /etc/hosts ; then echo "Hosts existing" &&  exit 1 ; fi

for svc in alert metrics grafana operator; do
    sudo bash -c "echo $node ${PREFIX}.${svc} | tee -a /etc/hosts"
    #echo $node ${PREFIX}.${svc}
done

