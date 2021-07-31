
terraform {
  required_providers {
    libvirt = {
      source = "dmacvicar/libvirt"
    }
  }

  backend "local" {
    # current folder if no absolute path
    path = "terraform.tfstate"
  }

}
