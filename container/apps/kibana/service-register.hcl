service {
  name = "kibana"
  id   = "kibana"
  checks = [
    {
        http = "http://127.0.0.1:5601/api/status"
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

