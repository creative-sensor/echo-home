include INPUT-SET


operator:
	helm install ${PREFIX}   stable/prometheus-operator


helm-notes:
	helm get notes ${PREFIX}


clean: 
	helm uninstall ${PREFIX}
	kubectl delete -f pv-0.${PREFIX}.yaml

scrape:
	helm upgrade ${PREFIX}   stable/prometheus-operator  -f values-scrape-configs.yaml
	#kubectl create secret generic additional-scrape-configs --from-file=custom-scrape-configs.yaml --dry-run -oyaml > additional-scrape-configs.yaml

ingress:
	sed -e "s/{{PREFIX}}/${PREFIX}/g" ingress.yaml > ingress.${PREFIX}.yaml
	kubectl apply -f ingress.${PREFIX}.yaml


host:
	./gen-hosts.sh ${PREFIX}


pv:
	sed -e "s/{{PREFIX}}/${PREFIX}/g" pv-0.yaml  > pv-0.${PREFIX}.yaml
	kubectl apply   -f pv-0.${PREFIX}.yaml
	helm upgrade ${PREFIX}   stable/prometheus-operator  -f values-pv.yaml
