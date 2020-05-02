namespace = default
pod = prober

prober:
	kubectl apply -f prober.yaml -n ${namespace}

clean:
	kubectl delete pod ${pod} -n ${namespace}

console:
	kubectl exec -it ${pod} -n ${namespace} -- /bin/bash

update:
	cat update.sh | $(MAKE) console
