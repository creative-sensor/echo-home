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
	cd $(subst _,/,$@)  ; set -x ;\
	    export MKGN_NAME=$@ ;\
	    export MKGN_INPUT=$$(grep  $@:  ${GRAPH_DIR}/Makefile) ;\
	    export MKGN_OUTPUT=${GRAPH_DIR}/$@ ;\
	    source .properd/VARSET ;\
		.function/artefact-get ;\
	    docker run -it  --rm \
	        --volume $$(pwd):/$${MKGN_NAME} \
	        --volume ${GRAPH_DIR}:/$${MKGN_NAME}/graph \
	        --workdir /$${MKGN_NAME} \
	        --env MKGN_OUTPUT=/$${MKGN_NAME}/mkgn.out \
	        --env MKGN_INPUT="$$MKGN_INPUT" \
	        --env MKGN_NAME=$$MKGN_NAME \
	        --env GRAPH_DIR=/$${MKGN_NAME}/graph \
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


define nodeX_objectory
endef


define nodeX_nomad_job
endef

define nodeX_healthcheck
endef

define nodeX_operator_configure
endef

define nodeX_gitlab_ci_job
endef

define nodeX_scheduler
endef

define nodeX_service_url
endef

define nodeX_ssh_remote
endef

define nodeX_grpc
endef

define nodeX_objectory
endef
