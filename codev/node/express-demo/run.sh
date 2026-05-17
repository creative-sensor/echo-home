#!/bin/bash

# Define the path to the root folder
# We use the version that matches your current setup
VERSION="18.20.4"
BASE_PATH="$LOCAL_BIN/node/$VERSION"

# Point NODE_PATH to the node_modules folder inside it
export NODE_PATH="$BASE_PATH/node_modules"

echo "Starting demo app..."
echo "Target Node Version: $VERSION"
echo "NODE_PATH: $NODE_PATH"

if [ ! -d "$BASE_PATH" ]; then
    echo "Error: Node installation not found at $BASE_PATH"
    echo "Please run your artefact-readi script for version $VERSION"
    exit 1
fi

node index.js
