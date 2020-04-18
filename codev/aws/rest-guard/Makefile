include INPUT-SET.remote


install_dir = /${ORG_NAME}/rest-guard/


install:
	mkdir -p ${install_dir} ; cp rest.sh ${install_dir}/
	cp rest-guard.* /usr/lib/systemd/system/
	sed -i 's/{{ORG_NAME}}/${ORG_NAME}/g' /usr/lib/systemd/system/rest-guard.service
	sed -i -e 's/{{FIRST_REST}}/${FIRST_REST}/g' \
	    -e 's/{{PERIODIC_REST}}/${PERIODIC_REST}/g'    /usr/lib/systemd/system/rest-guard.timer
	systemctl daemon-reload
	systemctl enable rest-guard.timer
	systemctl start rest-guard.timer


cli:
	mkdir -p ~/bin
	cat ./INPUT-SET > ~/bin/weikup
	cat ec2-wakeup >> ~/bin/weikup
	chmod 0700 ~/bin/weikup
	 

clean:
	systemctl stop rest-guard.timer
	rm -rf /usr/lib/systemd/system/rest-guard.*

awscli:
	yum install awscli -y
