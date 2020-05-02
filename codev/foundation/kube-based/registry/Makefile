include INPUT-SET

datum=./data/${PREFIX}

registry: pvc tls 
	sed -e "s/{{PREFIX}}/${PREFIX}/g" \
	    values.yaml > values.${PREFIX}.yaml
	cp values-secrets.yaml values-secrets.${PREFIX}.yaml
	helm install ${PREFIX}   stable/docker-registry -f values.${PREFIX}.yaml -f values-secrets.${PREFIX}.yaml


helm-notes:
	helm get notes ${PREFIX}


clean: 
	helm uninstall ${PREFIX}
	kubectl delete -f pv-0.${PREFIX}.yaml -f pvc.${PREFIX}.yaml -f tls-secret.${PREFIX}.yaml


host:
	./gen-hosts.sh ${PREFIX}


pv:
	sed -e "s/{{PREFIX}}/${PREFIX}/g" pv-0.yaml  > pv-0.${PREFIX}.yaml
	kubectl apply   -f pv-0.${PREFIX}.yaml


pvc: pv
	sed -e "s/{{PREFIX}}/${PREFIX}/g" pvc.yaml  > pvc.${PREFIX}.yaml
	kubectl apply -f pvc.${PREFIX}.yaml


tls:
	sed -e "s/{{PREFIX}}/${PREFIX}/g" \
	    -e "s/{{BASE64_TLS_CRT}}/${BASE64_TLS_CRT}/" \
	    -e "s/{{BASE64_TLS_KEY}}/${BASE64_TLS_KEY}/" \
	    tls-secret.yaml  > tls-secret.${PREFIX}.yaml
	kubectl apply -f tls-secret.${PREFIX}.yaml


datum:
	mkdir -p ./data/${PREFIX}


user: datum
	htpasswd -n -b -B $(USER) $(PASS) >> ${datum}/htpasswd 
	cp values-secrets.yaml values-secrets.${PREFIX}.yaml
	cat ${datum}/htpasswd | sed "s/^/    /"  >> values-secrets.${PREFIX}.yaml
	helm upgrade ${PREFIX} stable/docker-registry  -f values.${PREFIX}.yaml -f values-secrets.${PREFIX}.yaml

