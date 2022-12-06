log_level = "DEBUG"
# Enable service mesh
connect {
  enabled = true
}
# Addresses and ports
addresses {
  grpc = "0.0.0.0"
  http = "127.0.0.1"
  https = "0.0.0.0"
  dns = "0.0.0.0"
}


ports {
  serf_lan = 8311
  http = 8511
  https= 8501
  grpc_tls = 8512
  dns = 8611
}

# DNS recursors
recursors = ["1.1.1.1"]
# Disable script checks
enable_script_checks = false
# Enable local script checks
enable_local_script_checks = true

client_addr = "0.0.0.0"

