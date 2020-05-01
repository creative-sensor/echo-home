NAME=$1
NAMESPACE=$2

test -z ${NAMESPACE} && NAMESPACE=default

BOLD=' \e[1m' # Work only if "allow bold text" setting is enabled
STOP='\e[0m'

echo -e ${BOLD}"* STATUS -----"${STOP}
kubectl  get pod "${NAME}" -n ${NAMESPACE} -o=json | \
    jq ' .status | {"hostIP": .hostIP, "podIPs": .podIPs}'


echo -e ${BOLD}"* POD CONTAINTERS -----"${STOP}
kubectl  get pod "${NAME}" -n ${NAMESPACE} -o=json | \
    jq ' .spec.containers[] | { "Container": .name   ,   "Port": .ports[].containerPort   ,  "Command": .command }'


echo -e ${BOLD}"* SERVICE -----"${STOP}
./print-pod-with-svc.py ${NAME} ${NAMESPACE}
echo 

echo -e ${BOLD}"* INIT CONTAINTERS -----"${STOP}
kubectl  get pod "${NAME}" -n ${NAMESPACE} -o=json | \
    jq ' .spec.initContainers[] | { "Container": .name , "Args": .args }' 

echo -e ${BOLD}"----- -----"${STOP}
