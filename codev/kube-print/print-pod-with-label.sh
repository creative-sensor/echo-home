NAME=$1

BOLD=' \e[1m' # Work only if "allow bold text" setting is enabled
STOP='\e[0m'

kubectl  get pod "$NAME" -o=json | \
    jq ' .metadata.labels'


