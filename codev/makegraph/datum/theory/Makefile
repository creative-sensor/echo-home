
include ${MK_ENGINE}
root: node_P  node_B  node_DP  node_DP2 node_addition  node_n1  node_n2  node_subtraction  node_n3  node_JS 
node_P: node_DP
	$(nodeX_python)
#---node_P
node_B: node_DP2
	$(nodeX_bash)
#---node_B
node_DP: node_B node_DP2
	$(nodeX_docker_python)
#---node_DP
node_DP2:
	$(nodeX_docker_python)
#---node_DP2
node_addition: node_n1  node_subtraction 
	$(nodeX_bash)
#---node_addition
node_n1:
	$(nodeX_bash)
#---node_n1
node_n2:
	$(nodeX_bash)
#---node_n2
node_subtraction: node_n3  node_n2 
	$(nodeX_bash)
#---node_subtraction
node_n3:
	$(nodeX_bash)
#---node_n3
node_JS:
	$(nodeX_nodejs)
#---node_JS
