
serverless:
	npm install \
	    serverless \
	    serverless-offline
	ln -sf node_modules/serverless/bin/serverless.js serverless

run: test
	cd test ; ../serverless offline
