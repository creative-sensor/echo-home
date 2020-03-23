#!/bin/bash -xe

OPTION=$1
INVENTORY_FILE=./inventory

### Python is required

for i in master worker_1 worker_2 worker_3 ; do
    ansible_host=`cat  $INVENTORY_FILE | grep $i |  grep -o 'ansible_host=[0-9\.]\+' | awk -F '=' '{ print $2}'`
    user=`awk -F "=" '/ansible_ssh_user/{ print $2 }' $INVENTORY_FILE`
    port=`awk -F "=" '/ansible_ssh_port/{ print $2 }' $INVENTORY_FILE`
    key=`awk -F "=" '/ansible_ssh_private_key_file/{ print $2 }' $INVENTORY_FILE`
    if ! ssh -i $key -p $port $user@$ansible_host 'sudo apt update && sudo apt install python -y' ; then
        echo "Python failed to install"
        exit 1
    fi
done

### RUN PLAYBOOK TO BUILD ENV
ansible-playbook -v -i $INVENTORY_FILE ./plays.yaml${OPTION}


