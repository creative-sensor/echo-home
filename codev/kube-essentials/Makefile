bash_profile = ~/.bash_profile
kubectl_url = 
helm_url = https://get.helm.sh/helm-v3.2.0-linux-amd64.tar.gz


bin-dir:
	mkdir -p ~/bin 


kubectl: bin-dir
	if which kubectl ;then exit 1 ;fi
	curl -LO https://storage.googleapis.com/kubernetes-release/release/`curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt`/bin/linux/amd64/kubectl
	chmod 0700 kubectl
	mv kubectl ~/bin
	

helm: bin-dir
	if which helm ;then exit 1 ;fi
	curl -LO ${helm_url}
	tar xf *.gz
	cp linux-amd64/helm ~/bin
	chmod 0700 ~/bin/helm
	rm -rf linux-amd64 *.gz
	
	helm repo add stable https://kubernetes-charts.storage.googleapis.com/
