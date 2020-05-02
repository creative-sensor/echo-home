include INPUT-SET


operator:
	helm install ${PREFIX}   stable/prometheus-operator  -f values.yaml

update:
	helm upgrade ${PREFIX} stable/prometheus-operator -f values.yaml

clean:
	helm uninstall ${PREFIX}

scrape:
	kubectl create secret generic additional-scrape-configs --from-file=custom-scrape-configs.yaml --dry-run -oyaml > additional-scrape-configs.yaml

host:
	#192.168.15.6 prome.alert
	#192.168.15.6 prome.metrics
	#192.168.15.6 prome.grafana
	#192.168.15.6 prome.operator
