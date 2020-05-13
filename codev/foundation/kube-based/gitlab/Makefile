

PREFIX=glab

gitlab:
	helm upgrade --install ${PREFIX} gitlab/gitlab \
	    --timeout 600s \
	    --set global.hosts.domain=glab.gitlab \
	    --set global.hosts.externalIP=10.10.10.10 \
	    --set certmanager-issuer.email=me@example.com

helm:
	helm repo add gitlab https://charts.gitlab.io/
	helm repo update


clean:
	helm uninstall ${PREFIX}
