# --- REMOTE VPN SERVER ---
ORG_NAME = anonymous
LISTEN_IP = 127.0.0.18
LISTEN_PORT = 443
PROTOCOL = tcp
SERVER_NAME = impersonator
TLS_AUTH_KEY = ta.key
PKI_CA_CERT = ca.crt
PKI_CLIENT_CERT = ${SERVER_NAME}.crt
PKI_CLIENT_KEY = ${SERVER_NAME}.key
DH = dh.pem


# --- SSH ACCESS ---
SSH_PUBLIC_IP = 8.8.8.8
SSH_KEY = ~/.ssh/id_rsa
SSH_USER = linux-user 
SSH_PORT = 22

