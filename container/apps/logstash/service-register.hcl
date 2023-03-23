service {
  name = "logstash"
  id   = "logstash"
  checks = [
    {
        http = "http://127.0.0.1:9600/?pretty"
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

