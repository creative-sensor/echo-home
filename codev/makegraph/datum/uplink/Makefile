
include ${MK_ENGINE}
root: node_ca_gen  node_consul_principal  node_consul_satellite  node_nomad_principal  node_nomad_satellite  node_dns127 

node_ca_gen:
	$(nodeX_objectory)
#---node_ca_gen
node_consul_principal:
	$(nodeX_objectory)
#---node_consul_principal
node_consul_satellite: node_consul_principal 
	$(nodeX_objectory)
#---node_consul_satellite
node_nomad_principal: node_consul_satellite 
	$(nodeX_objectory)
#---node_nomad_principal
node_nomad_satellite: node_nomad_principal 
	$(nodeX_objectory)
#---node_nomad_satellite
node_dns127: node_consul_satellite 
	$(nodeX_bash)
#---node_dns127
