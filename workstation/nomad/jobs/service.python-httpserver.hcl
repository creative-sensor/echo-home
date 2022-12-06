job "python-httpserver" {
    datacenters = ["dc1"]
    type = "service"
    group "python" {
        count = 1
        service {
            port = "8182"
            check {
                type     = "http"
                path     = "/"
                interval = "10s"
                timeout  = "2s"
            }
        }
        task "httpserver" {
            driver = "exec"
            config {
                command = "/usr/bin/python3"
                args = [
                    "-m" , "http.server",
                    "8182"
                ]
            }
        }
    }
}
