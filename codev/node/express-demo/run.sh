#!/bin/bash

# Define the path to the root folder
BASE_PATH="$LOCAL_BIN/node/18.20.4"

# Point NODE_PATH to the node_modules folder inside it
export NODE_PATH="$BASE_PATH/node_modules"

echo "Starting demo app with NODE_PATH=$NODE_PATH"
node index.js

