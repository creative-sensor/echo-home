#!/bin/bash

# Define the path to the express package provided in the request
EXPRESS_PATH="$LOCAL_BIN/node/18.20.4/express"

# Check if the path exists
if [ ! -d "$EXPRESS_PATH" ]; then
    echo "Error: Express package not found at $EXPRESS_PATH"
    echo "Please ensure the package is deployed correctly."
    exit 1
fi

# Set NODE_PATH to include the express directory so 'require(express)' works
# We add the directory containing the package to NODE_PATH
export NODE_PATH="$EXPRESS_PATH:$NODE_PATH"

echo "Starting demo app with NODE_PATH=$NODE_PATH"
node index.js
