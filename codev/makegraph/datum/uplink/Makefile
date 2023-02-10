
include ${MK_ENGINE}
root: node_ca_gen  node_consul_principal  node_consul_satellite  node_nomad_principal  node_nomad_satellite  node_dns127  node_consul_bootstrap_token  node_nomad_bootstrap_token  node_dns_iptables  node_consul_agent_token  node_nomad_set_consul_agent_token 

node_ca_gen:
	$(nodeX_objectory)
#---node_ca_gen
node_consul_principal: node_ca_gen 
	$(nodeX_objectory)
#---node_consul_principal
node_consul_satellite: node_consul_principal  node_consul_bootstrap_token  node_consul_agent_token  node_ca_gen 
	$(nodeX_objectory)
#---node_consul_satellite
node_nomad_principal: node_consul_satellite  node_nomad_set_consul_agent_token 
	$(nodeX_objectory)
#---node_nomad_principal
node_nomad_satellite: node_nomad_principal  node_dns_iptables  node_nomad_bootstrap_token 
	$(nodeX_objectory)
#---node_nomad_satellite
node_dns127: node_consul_satellite 
	$(nodeX_bash)
#---node_dns127
node_consul_bootstrap_token: node_consul_principal 
	$(nodeX_objectory)
#---node_consul_bootstrap_token
node_nomad_bootstrap_token: node_nomad_principal
	$(nodeX_objectory)
#---node_nomad_bootstrap_token
node_dns_iptables: node_dns127 
	$(nodeX_objectory)
#---node_dns_iptables
node_consul_agent_token: node_consul_bootstrap_token 
	$(nodeX_objectory)
#---node_consul_agent_token
node_nomad_set_consul_agent_token: node_consul_agent_token 
	$(nodeX_bash)
#---node_nomad_set_consul_agent_token
