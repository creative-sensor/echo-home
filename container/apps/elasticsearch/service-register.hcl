service {
  name = "elasticsearch"
  id   = "elasticsearch"
  checks = [
    {
        http = "http://127.0.0.1:9200/_cluster/health"
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

