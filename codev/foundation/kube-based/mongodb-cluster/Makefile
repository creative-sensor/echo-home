prefix = mgdb
cluster: pv
	helm install ${prefix} stable/mongodb-replicaset -f values.yaml	

pv:
	#mkdir /data* and chmod 777
	kubectl apply   -f pv-0.yaml   -f pv-1.yaml   -f pv-2.yaml

clean:
	./clean.sh ${prefix}

status:
	kubectl get pod
	kubectl get pvc
	kubectl get pv
