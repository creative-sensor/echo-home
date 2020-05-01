SVC_NAME=$1
label_name=$2
label_value=$3
NAMESPACE=$4

test -z ${NAMESPACE} && NAMESPACE=default

function backend(){
    kubectl  get pod -n ${NAMESPACE}  -o json | \
        jq '.items[] | select(.metadata.labels."'${label_name}'" == "'${label_value}'") | .metadata.name'
}


backend
