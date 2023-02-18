service {
  name = "vault"
  id   = "vault"
  check = {
    http = "http://127.0.0.1:8200/v1/sys/health"
    interval = "10s"
    timeout = "1s"
  }
}

