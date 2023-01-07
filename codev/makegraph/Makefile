# ---- NODEx ENGINE ----
SHELL:=/bin/bash
define print_marker
	echo -e "\n\n\n\e[38;5;34m---- $@ ----\e[0m" ;
endef

define nodeX_bash
	$(print_marker)
	cd $(subst _,/,$@)  ;\
	  export MKGN_NAME=$@ ;\
	  export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	  export MKGN_OUTPUT=${GRAPH_DIR}/$@ ;\
 	  .function/start
	touch $@
endef

define nodeX_docker
	$(print_marker)
	cd $(subst _,/,$@)  ;\
		export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	   	export MKGN_OUTPUT=${GRAPH_DIR}/$@ ;\
	   	docker ps > $$MKGN_OUTPUT
	touch $@
endef

define nodeX_docker_python
	$(print_marker)
	touch $@
	cd $(subst _,/,$@)  ;\
	  	export MKGN_NAME=$@ ;\
		export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	   	export MKGN_OUTPUT=${GRAPH_DIR}/$@ ; set -x;\
	    source .properd/VARSET ;\
		docker run -it  --rm \
		    --volume $$(pwd):/$${MKGN_NAME} \
			--workdir /$${MKGN_NAME} \
			--env MKGN_OUTPUT=/$${MKGN_NAME}/mkgn.out \
			--env MKGN_INPUT="$$MKGN_INPUT" \
			--env MKGN_NAME=$$MKGN_NAME \
			--mount type=bind,source="$$MKGN_OUTPUT",target="/$${MKGN_NAME}/mkgn.out" \
			$${DKR_IMAGE} \
	            python .function/start
endef

define nodeX_python
	$(print_marker)
	cd $(subst _,/,$@)  ;\
	  	export MKGN_NAME=$@ ;\
		export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	   	export MKGN_OUTPUT=${GRAPH_DIR}/$@ ;\
	   	python .function/start
	touch $@
endef
