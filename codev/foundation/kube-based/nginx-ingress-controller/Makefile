
prefix = ngx-ingress
kube_ns = default 

controller:
	helm install   ${prefix}  stable/nginx-ingress

clean:
	helm uninstall ${prefix}


nginx:
	#helm repo add bitnami https://charts.bitnami.com/bitnami
	helm install ngix bitnami/nginx
