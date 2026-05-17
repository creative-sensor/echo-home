#!/bin/bash

# Define the path to the express package provided in the request
EXPRESS_PATH="$LOCAL_BIN/node/18.20.4/express"

# Check if the path exists
if [ ! -d "$EXPRESS_PATH" ]; then
    echo "Error: Express package not found at $EXPRESS_PATH"
    echo "Please ensure the package is deployed correctly."
    exit 1
fi

# Get the parent directory of the express folder.
# NODE_PATH must point to the directory containing the 'express' folder.
EXPRESS_PARENT_DIR=$(dirname "$EXPRESS_PATH")

# Set NODE_PATH to include the parent directory so 'require(express)' works
# Also include the node_modules inside the express folder to resolve its dependencies
export NODE_PATH="$EXPRESS_PARENT_DIR"

echo "Starting demo app with NODE_PATH=$NODE_PATH"
node index.js
