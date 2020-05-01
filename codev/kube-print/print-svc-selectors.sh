SVC_NAME=$1
NAMESPACE=$2


function selector(){
    test -z ${NAMESPACE} && NAMESPACE=default
    selector_json=$(kubectl   get  svc ${SVC_NAME} -n ${NAMESPACE} -o json | jq '.spec.selector')
    echo $selector_json
}


selector

