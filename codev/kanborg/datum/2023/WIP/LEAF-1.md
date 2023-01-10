# CODEV : Makefile as graph workflow
--------------------------------
### 0 DESCRIPTION
Attempt to use Makefile to build graph , workflow , relation map , dependency

### 1 SOLUTION


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 DISCUSSION
###### 4.1
Passing Input and set outut for each node

```Makefile
define nodeX
	echo -e "\e[38;5;34m ---- $@ ---- \e[0m" ;
	cd $(subst _,/,$@)  ;\
		export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	   	export MKGN_OUTPUT=${GRAPH_DIR}/$@ ;\
	   	./run
	touch $@
endef
```

###### 4.0
Make is likely to  create a workflow of nodes, each of which can represent a specific type of execution such as docker container, windows box, remote service-url endpoint


<img alt="" src="https://raw.githubusercontent.com/creative-sensor/echo-home/8787c01c209495b9dbb03d2ec5e5c645a4f4a3b6/codev/makegraph/graph.svg"/>

```Makefile
# ---- ROOT ----
define nodeX
	echo "---- $@ ----" ;
	cd $(subst _,/,$@) ; ./run
	touch $@
endef

root: node_1 node_2 node_3 node_A node_B node_C node_H



# ---- NODE ----
node_1:
	$(nodeX)


node_2:
	$(nodeX)


node_3:
	$(nodeX)


node_A: node_3
	$(nodeX)


node_B: node_1 node_2 node_C
	$(nodeX)


node_C: node_2 node_3
	$(nodeX)


node_H:
	$(nodeX)

```


--------------------------------
```json
{ "project_code": "LEAF" , "links": "" , "location": "" , "fpoint": "" }
```
