service {
  name = "gitlab-happy"
  id   = "gitlab-happy"
  checks = [
    {
        http = "http://127.0.0.1:80/-/health"
        interval = "10s"
        timeout = "1s"
    },
    {
        http = "http://127.0.0.1:80/-/readiness?all=1"
        interval = "10s"
        timeout = "1s"
    },
    {
        http = "http://127.0.0.1:80/-/liveness"
        interval = "10s"
        timeout = "1s"
    }
  ]
  
}

