service {
  name = "haproxy"
  id   = "haproxy"
  checks = [
    {
        http = "http://127.0.0.1:8404/stats"
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

