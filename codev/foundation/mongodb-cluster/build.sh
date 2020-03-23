#!/bin/bash -xe

OPTION=$1
INVENTORY_FILE=./inventory

### Python is required

for i in {1..3} ; do
    ansible_host=`awk -F "="    '/node_'${i}'/{ print $2 }'    $INVENTORY_FILE`
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


