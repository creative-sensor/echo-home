#--------------- INPUT/CONFIGURE ---------------
include INPUT-CONFIGURE.mk
#-----------------------------------------------


systemd_service_path = /usr/lib/systemd/system/openvpn.service
vpn_config_path = /${ORG_NAME}/vpn/vpn.conf
interface = "$(shell ip addr | grep "inet ${LISTEN_IP}" | grep -o  "[^ ]\+$$")"


remote-install: systemd-service  openvpn-server iptable-config
	

openvpn-server: vpn.conf  ${PKI_CA_CERT} ${PKI_CLIENT_CERT} ${PKI_CLIENT_KEY}
	ip addr | grep "inet ${LISTEN_IP}" || exit 1
	# test if current host has LISTEN_IP

	which openvpn || yum install -y openvpn
	mkdir -p /${ORG_NAME}/vpn
	cp  ${PKI_CA_CERT} ${PKI_CLIENT_CERT} ${PKI_CLIENT_KEY}    /${ORG_NAME}/vpn/
	cp  ${TLS_AUTH_KEY}  /${ORG_NAME}/vpn/ta.key
	cp  ${DH}  /${ORG_NAME}/vpn/dh.pem

	sed -e "s/{{SERVER_NAME}}/${SERVER_NAME}/g" \
	    -e "s/{{ORG_NAME}}/${ORG_NAME}/g" \
	    -e "s/{{LISTEN_IP}}/${LISTEN_IP}/g" \
	    -e "s/{{PROTOCOL}}/${PROTOCOL}/g" \
	    -e "s/{{LISTEN_PORT}}/${LISTEN_PORT}/g" \
	    vpn.conf > ${vpn_config_path}

	chcon  -t openvpn_etc_rw_t /${ORG_NAME}/vpn/* || echo "skip chcon"


iptable-config:
	grep ^net.ipv4.ip_forward=1 /etc/sysctl.conf || \
		echo  net.ipv4.ip_forward=1 >> /etc/sysctl.conf
	sysctl -w net.ipv4.ip_forward=1

	systemctl enable iptables
	systemctl start iptables
	cp /etc/sysconfig/iptables /etc/sysconfig/.iptables.backup
	iptables -t nat -A POSTROUTING -o ${interface} -j MASQUERADE
	service iptables save


systemd-service: openvpn.service  openvpn-server
	sed -e "s/{{SERVER_NAME}}/${SERVER_NAME}/g" \
	    -e "s/{{ORG_NAME}}/${ORG_NAME}/g" \
	    openvpn.service > ${systemd_service_path}
	systemctl daemon-reload
	systemctl enable openvpn.service
	systemctl start openvpn.service


ansible-inventory: INVENTORY.yaml
	sed -e "s/{{SSH_PUBLIC_IP}}/${SSH_PUBLIC_IP}/g" \
	    -e "s,{{SSH_KEY}},${SSH_KEY},g" \
	    -e "s/{{SSH_USER}}/${SSH_USER}/g" \
	    -e "s/{{SSH_PORT}}/${SSH_PORT}/g" \
	    INVENTORY.yaml > INVENTORY.${ORG_NAME}.yaml

	 
vpn-server-endpoint: ansible-inventory  plays.yaml  ansible-tool
	ansible-playbook -i INVENTORY.${ORG_NAME}.yaml   plays.yaml 


ansible-tool:
	rpm -qi  epel-release || sudo yum install epel-release -y
	which ansible-playbook || sudo yum install ansible -y


remote-clean:
	ip addr | grep "inet ${LISTEN_IP}" || exit 1
	systemctl stop openvpn.service
	rm ${systemd_service_path}
	rm -rf /${ORG_NAME}
	sed -i "s/^net.ipv4.ip_forward=1//g" /etc/sysctl.conf
	cp /etc/sysconfig/.iptables.backup /etc/sysconfig/iptables

