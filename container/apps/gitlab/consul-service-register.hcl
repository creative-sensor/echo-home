service {
  name = "gitlab"
  id   = "gitlab"
  checks = [
    {
        http = "http://127.0.0.1/users/sign_in"
        interval = "10s"
        timeout = "1s"
    },
  ]
  
}

