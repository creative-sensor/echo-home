service {
  name = "authelia"
  id   = "authelia"
  checks = [
    {
        http = "https://172.17.0.1:9091/api/health"
        tls_server_name =  "authelia"
        tls_skip_verify = true
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

