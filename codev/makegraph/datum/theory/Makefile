
include ${MK_ENGINE}
root:     node_P  node_B node_D node_DP







node_P: node_B node_D
	$(nodeX_python)


node_B:
	$(nodeX_bash)

node_D: node_DP
	$(nodeX_docker)

node_DP: node_B
	$(nodeX_docker_python)

