#--------------------------------
include INPUT-SET.mk
#--------------------------------

datum = ./data
ca_dir = ${datum}/${ISSUER}
subject_dir = ${ca_dir}/subjects/${SUBJECT}
subject_openssl_config = openssl.cnf.${ORG_NAME}.${SUBJECT}
initial_serial = CCCCAAAA77770000





ca-openssl-config:
	sed -e "s/{{EXTENSION}}/${EXTENSION}/" \
	    -e "s/{{CA_NAME}}/${ISSUER}/" \
	    -e "s/{{SUBJECT}}/${ISSUER}/" \
	    -e "s/{{ORG_NAME}}/AUTHORITY/" \
	    openssl.cnf  >   openssl.cnf.ca.${ISSUER}

openssl-config:
	sed -e "s/{{EXTENSION}}/${EXTENSION}/" \
	    -e "s/{{CA_NAME}}/${ISSUER}/" \
	    -e "s/{{SUBJECT}}/${SUBJECT}/" \
	    -e "s/{{ORG_NAME}}/${ORG_NAME}/" \
	    openssl.cnf  >   ${subject_openssl_config}


ca: ca-openssl-config
ifeq ("$(wildcard ${ca_dir}/ca.key.pem)","")
	mkdir -p ${ca_dir}
	mkdir -p ${ca_dir}/cert-db
	touch  ${ca_dir}/index.txt
	echo ${initial_serial}  > ${ca_dir}/serial
	./randfile-gen.sh > ${datum}/.rnd

	openssl  req -config openssl.cnf.ca.${ISSUER} \
	    -x509 \
	    -days 1825 \
	    -newkey rsa:4096 \
	    -keyout ${ca_dir}/ca.key.pem \
	    -out ${ca_dir}/ca.cert.pem
else
	echo "Using existing CA: ${ISSUER}"
endif


ca-about: ca
	openssl x509  -text  -noout -in ${ca_dir}/ca.cert.pem


ca-list:
	tree -d data


csr: openssl-config
ifeq ("$(wildcard ${subject_dir}/key.pem)","")
	mkdir -p ${subject_dir}
	./randfile-gen.sh > ${datum}/.rnd
	openssl req -config ${subject_openssl_config} \
	    -new \
	    -keyout "${subject_dir}/key.pem" \
	    -out "${subject_dir}/csr.pem"
else
	echo "CSR existing"
endif


csr-about: csr
	openssl req -text -noout -verify -in ${subject_dir}/csr.pem


ca-signing: openssl-config   ca   csr
	openssl ca -config ${subject_openssl_config} \
	    -in ${subject_dir}/csr.pem \
	    -out ${subject_dir}/cert.pem


cert-about: ${subject_dir}/cert.pem
	openssl x509 -purpose  -text  -noout -in ${subject_dir}/cert.pem
	    

openssl-tools:
	which openssl || yum install openssl -y


generate-csr:
	openssl x509 \
       -in domain.crt \
       -signkey domain.key \
       -x509toreq -out domain.csr


subject-verify:
	cert_sum=$$(openssl x509 -noout -modulus -in ${subject_dir}/cert.pem | sha256sum -); \
	key_sum=$$(openssl rsa -noout -modulus -in ${subject_dir}/key.pem   | sha256sum -); \
	test "$$cert_sum" = "$$key_sum" && echo "Subject key and certificate are matching OK"

	openssl verify -verbose -CAfile ${ca_dir}/ca.cert.pem  ${subject_dir}/cert.pem


ca-remove: ${ca_dir}
	echo "Removing issuer ${ISSUER} in 60s ..." ; sleep 60
	test -z "${ISSUER}" || rm -rf "${ca_dir}"


subject-export:
	mkdir -p ${subject_dir}/tmp 
	rm -rf ./${subject_dir}/tmp/*
	cp ${subject_dir}/cert.pem    ${subject_dir}/tmp/${SUBJECT}.cert.pem
	cp ${subject_dir}/key.pem    ${subject_dir}/tmp/${SUBJECT}.key.pem
	cp ${ca_dir}/ca.cert.pem    ${subject_dir}/tmp/ca.cert.${ISSUER}.pem
	if test -s ${ca_dir}/crl.pem ; then cp ${ca_dir}/crl.pem   ${subject_dir}/tmp/crl.pem ; fi
	cd ${subject_dir}/tmp;   zip ${ORG_NAME}.${SUBJECT}.zip    *.pem ; \
	    echo "EXPORTED:";    realpath ${ORG_NAME}.${SUBJECT}.zip


subject-revoke:
	echo "Removing subject ${SUBJECT} in 30s ..." ; sleep 30

	openssl ca -config  ${subject_openssl_config} \
	    -revoke ${subject_dir}/cert.pem
	openssl ca -config ${subject_openssl_config} \
	    -gencrl -out ${ca_dir}/crl.pem

	rm -rf ${subject_dir}


crl-about:
	openssl crl  -text -noout -in ${ca_dir}/crl.pem

