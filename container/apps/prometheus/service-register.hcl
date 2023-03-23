service {
  name = "prometheus"
  id   = "prometheus"
  checks = [
    {
        http = "http://127.0.0.1:9090/-/healthy"
        interval = "10s"
        timeout = "1s"
    },
    {
        http = "http://127.0.0.1:9090/-/ready"
        interval = "10s"
        timeout = "1s"
    },

  ]
  
}

