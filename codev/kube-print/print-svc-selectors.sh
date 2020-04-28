SVC_NAME=$1

function selector(){
    selector_json=$(kubectl   get  svc ${SVC_NAME} -o json | \
        jq '.spec.selector | to_entries')
    echo $selector_json
}


selector

