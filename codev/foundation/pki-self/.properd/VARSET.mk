VARSET = $(shell VARSET_EXT=${VARSET_EXT} ;  echo $${VARSET_EXT:-.properd/VARSET})
ISSUER = $(shell ISSUER=yykey ; source ${VARSET} ;  echo $$ISSUER)
ORG_NAME = $(shell ORG_NAME=rangeroo ; source ${VARSET} ;  echo $$ORG_NAME)
SUBJECT = $(shell SUBJECT=mutant ; source ${VARSET} ;  echo $$SUBJECT)
EXTENSION = $(shell EXTENSION=standard ; source ${VARSET} ;  echo $$EXTENSION)
datum = $(shell DATUM_DIR=datum ; source ${VARSET} ; echo $$DATUM_DIR)
ca_dir = ${datum}/${ISSUER}
subject_dir = ${ca_dir}/subjects/${SUBJECT}
subject_openssl_config = openssl.cnf.${ORG_NAME}.${SUBJECT}
initial_serial =  $(shell INITIAL_SERIAL=$$(openssl rand -hex 8) ; source ${VARSET} ; echo $${INITIAL_SERIAL^^})

