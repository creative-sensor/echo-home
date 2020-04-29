SVC_NAME=$1
label_name=$2
label_value=$3


function backend(){
    kubectl  get pod  -o json | \
        jq '.items[] | select(.metadata.labels."'${label_name}'" == "'${label_value}'") | .metadata.name'
}


backend
