NAME=$1

BOLD=' \e[1m' # Work only if "allow bold text" setting is enabled
STOP='\e[0m'

echo -e ${BOLD}"* STATUS -----"${STOP}
kubectl  get pod "$NAME" -o=json | \
    jq ' .status | {"hostIP": .hostIP, "podIPs": .podIPs}'


echo -e ${BOLD}"* POD CONTAINTERS -----"${STOP}
kubectl  get pod "$NAME" -o=json | \
    jq ' .spec.containers[] | { "Container": .name   ,   "Port": .ports[].containerPort   ,  "Command": .command }'


echo -e ${BOLD}"* SERVICE -----"${STOP}
./print-pod-with-svc.py $NAME
echo 

echo -e ${BOLD}"* INIT CONTAINTERS -----"${STOP}
kubectl  get pod "$NAME" -o=json | \
    jq ' .spec.initContainers[] | { "Container": .name , "Args": .args }' 

echo -e ${BOLD}"----- -----"${STOP}
