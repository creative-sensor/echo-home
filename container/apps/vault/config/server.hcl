storage "file" {
  path = "/objectory/datum"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

ui = true
#telemetry {
#  statsite_address = "127.0.0.1:8125"
#  disable_hostname = true
#}


