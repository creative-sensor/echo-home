include INPUT-SET

datum = ./data/${PREFIX}
#debug: TF_LOG=TRACE

CMD=plan

vpc: terraform
	if ! [[ "${PREFIX}" =~ ^[a-z0-9]{3,9}$$ ]] ; then echo "Regex: no match"; exit 1 ; fi
	mkdir -p ${datum}/${PREFIX}
	sed -e "s/{{PREFIX}}/${PREFIX}/g" \
	    -e "s/{{OCTET}}/${OCTET}/g" \
	    vpc.tf > ${datum}/vpc.tf
	sed -e "s/{{PREFIX}}/${PREFIX}/g" route.tf > ${datum}/route.tf
	
	source ./TF_VARS ;\
	    cd ${datum} && terraform init && terraform ${CMD}

terraform:
	if ! which terraform ; then ./tf-setup.sh ; fi
	
