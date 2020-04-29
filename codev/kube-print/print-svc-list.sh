
list=$(kubectl  get svc -o json | jq ' .items[].metadata.name')
echo -n ${list//\"/}
