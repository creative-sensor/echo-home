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

    volume "datum_host" {
      type      = "host"
      read_only = false
      source    = "datum_host"
    }

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

      volume_mount {
        volume      = "datum_host"
        destination = "/var/lib/grafana"
        read_only   = false
      }

      env {
        ENVAR = "envalue"
        #GF_PATHS_CONFIG = "/etc/grafana/grafana.ini"
        #GF_PATHS_DATA = "/var/lib/grafana"
        #GF_PATHS_HOME = "/usr/share/grafana"
        #GF_PATHS_LOGS = "/var/log/grafana"
        #GF_PATHS_PLUGINS = "/var/lib/grafana/plugins"
        #GF_PATHS_PROVISIONING = "/etc/grafana/provisioning"

      }

      resources {
        cpu    = 500 # MHz
        memory = 256 # MB
      }
    }
  }
}
