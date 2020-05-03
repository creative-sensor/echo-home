This page presents usage of prometheus-operator atop kube cluster

### 0 INPUT
* Edit ```INPUT-SET```: helm prefix and kube namespace


### 1 INSTALL
Install operator:
```bash
make operator
```

### 2 INGRESS
Create ingress for prometheus operator:
```bash
make ingress
```


### 3 PERSISTENT VOLUME
Store prometheus data in local hostpath.

* Create data directory: run ```mkdir-data.sh``` on each kube node
* Create volume:
```
make pv
```


### 4 ADDITIONAL SCRAPE
Append scrape-config in  ```values-scrape-configs.yaml``` :
```bash
make scrape
```


