#!/bin/bash
set -e

# Create infrastructure
echo "Creating volumes and network..."
podman volume create mg_lib || true
podman volume create mg_log || true
podman network create memgraph-net || true

# Start Memgraph
echo "Starting memgraph container..."
podman run -d \
  --name memgraph \
  --network memgraph-net \
  -p 7687:7687 \
  -p 7444:7444 \
  -v mg_lib:/var/lib/memgraph \
  -v mg_log:/var/log/memgraph \
  --restart unless-stopped \
  docker.io/memgraph/memgraph-mage:latest \
  --log-level=INFO --schema-info-enabled=True

# Start Memgraph Lab
echo "Starting memgraph-lab container..."
podman run -d \
  --name memgraph-lab \
  --network memgraph-net \
  -p 3000:3000 \
  -e QUICK_CONNECT_MG_HOST=memgraph \
  -e QUICK_CONNECT_MG_PORT=7687 \
  --restart unless-stopped \
  docker.io/memgraph/lab:latest

echo "Setup complete!"
