NAME=$1
NAMESPACE=$2

BOLD=' \e[1m' # Work only if "allow bold text" setting is enabled
STOP='\e[0m'

test -z ${NAMESPACE} && NAMESPACE=default
kubectl  get pod "$NAME" -n ${NAMESPACE} -o=json | \
    jq ' .metadata.labels'


