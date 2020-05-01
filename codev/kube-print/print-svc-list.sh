NAMESPACE=$1
test -n "${NAMESPACE}" && NAMESPACE="-n "${NAMESPACE}
test -z "${NAMESPACE}" && NAMESPACE="-A"
list=$(kubectl  get svc $NAMESPACE -o json | jq ' .items[].metadata.name')
echo -n ${list//\"/}
