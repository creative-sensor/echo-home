#--------------- INPUT/CONFIGURE ---------------
include INPUT-CONFIGURE.mk
#-----------------------------------------------


systemd_service_path = /usr/lib/systemd/system/openvpn.service
vpn_config_path = /${ORG_NAME}/vpn/vpn.conf


install: systemd-service  openvpn-client
	

openvpn-client: vpn.conf  ${PKI_CA_CERT} ${PKI_CLIENT_CERT} ${PKI_CLIENT_KEY}
	which openvpn || yum install -y openvpn
	mkdir -p /${ORG_NAME}/vpn
	cp  ${PKI_CA_CERT} ${PKI_CLIENT_CERT} ${PKI_CLIENT_KEY}    /${ORG_NAME}/vpn/
	cp ${TLS_AUTH_KEY} /${ORG_NAME}/vpn/ta.key

	sed -e "s/{{CLIENT_NAME}}/${CLIENT_NAME}/g" \
	    -e "s/{{ORG_NAME}}/${ORG_NAME}/g" \
        -e "s/{{REMOTE_ENDPOINT}}/${REMOTE_ENDPOINT}/g" \
        -e "s/{{PROTOCOL}}/${PROTOCOL}/g" \
        -e "s/{{PORT}}/${PORT}/g" \
        vpn.conf > ${vpn_config_path}

	chcon  -t openvpn_etc_rw_t /${ORG_NAME}/vpn/*



systemd-service: openvpn.service openvpn-client
	sed -e "s/{{CLIENT_NAME}}/${CLIENT_NAME}/g" \
	    -e "s/{{ORG_NAME}}/${ORG_NAME}/g" \
	    openvpn.service > ${systemd_service_path}
	systemctl daemon-reload
	systemctl enable openvpn.service
	systemctl start openvpn.service

clean:
	systemctl stop openvpn.service
	rm ${systemd_service_path}
	rm -rf /${ORG_NAME}
