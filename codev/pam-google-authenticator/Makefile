pam_config = auth required pam_google_authenticator.so

ssh_config_passwd_yes = PasswordAuthentication yes
ssh_config_challenge_no = ChallengeResponseAuthentication no
ssh_config_auth_method = AuthenticationMethods  publickey,keyboard-interactive


pam_ssh_config_auth_passwd = auth       substack     password-auth
pam_ssh_config_auth_google = auth sufficient pam_google_authenticator.so



google-authenticator:
	dnf list --installed google-authenticator &> /dev/null || sudo dnf install google-authenticator -y


key-setup: google-authenticator
	test -s ~/.google_authenticator || google-authenticator --time-based


gdm-2fa: key-setup
	pam_path=/etc/pam.d/gdm-password ; \
	grep "^${pam_config}" $${pam_path} || sudo bash -c "echo ${pam_config} >> $${pam_path}"


tty-2fa: key-setup
	pam_path=/etc/pam.d/login ; \
	grep "^${pam_config}" $${pam_path} || sudo bash -c "echo ${pam_config} >> $${pam_path}"


sshd-2fa: key-setup
	#PAM
	sudo sed  -e 's/^${pam_ssh_config_auth_passwd}/#${pam_ssh_config_auth_passwd}/' \
	          -e '$$ a ${pam_ssh_config_auth_google}' \
	          -i /etc/pam.d/sshd

	#SSHD
	sudo sed  -e 's/^${ssh_config_passwd_yes}/PasswordAuthentication no/' \
	          -e 's/^${ssh_config_challenge_no}/ChallengeResponseAuthentication yes/' \
	          -e '$$ a ${ssh_config_auth_method}' \
	          -i /etc/ssh/sshd_config

	sudo systemctl restart sshd

#sudo: NOPASSWD
