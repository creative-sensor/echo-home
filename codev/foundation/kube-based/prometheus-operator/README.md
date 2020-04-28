This page presents usage of prometheus-operator atop kube cluster

### 0 INPUT
* Edit ```values.yaml```: add scape config as needed
* Edit ```INPUT-SET```: helm prefix and kube namespace


### 1 INSTALL
```bash
bash> make operator
```

### 2 UPDATE VALUES
Prometheus can be updated with new values in yaml file
```bash
bash> make update
```

### 3 ADDITIONAL SCRAPE BY SECRET CONFIG
TODO: make scrape


