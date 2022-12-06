job "grafana-dashboard" {
  #region = "us"

  datacenters = ["dc1"]

  type = "service"

  update {
    stagger      = "30s"
    max_parallel = 2
  }


  group "grafana" {
    count = 1
    network {
      # This requests a dynamic port named "http". This will
      # be something like "46283", but we refer to it via the
      # label "http".
      port "http" {
        static = 3000
      }

      # This requests a static port on 443 on the host. This
      # will restrict this task to running once per host, since
      # there is only one port 443 on each host.
      port "https" {
        #static = 3000
      }
    }

    # The service block tells Nomad how to register this service
    # with Consul for service discovery and monitoring.
    service {
      port = "http"
      check {
        type     = "http"
        path     = "/api/health"
        interval = "10s"
        timeout  = "2s"
      }
    }

    task "grafana-docker" {
      driver = "docker"
      config {
        image = "grafana/grafana-oss:8.2.0"
        ports = ["http", "https"]
      }
      
      env {
        ENVAR = "envalue"
      }

      resources {
        cpu    = 500 # MHz
        memory = 256 # MB
      }
    }
  }
}
