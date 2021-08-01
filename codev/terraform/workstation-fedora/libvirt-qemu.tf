locals {
  DOMAIN_NAME                = "workstation-fedora"
  FEDORA_VERSION             = 32
  POOL_PATH                  = pathexpand("/datum/vdisk")
  ISO_DISK                   = pathexpand("/datum/vdisk/Fedora-Workstation-Live-x86_64-32-1.6.iso")
  SYSTEM_DISK_SIZE_IN_BYTE   = 8000000
  SYSTEM_DISK_REFERENCE      = "false"
  SYSTEM_DISK_REFERENCE_PATH = pathexpand("/datum/vdisk/workstation-fedora-32.raw.img")
}


# -------- RESOURCES --------

provider "libvirt" {
  uri = "qemu:///system"
}


resource "random_string" "suffix" {
  length  = 4
  special = false
}

resource "libvirt_pool" "vdisk" {
  name = "vdisk"
  type = "dir"
  path = local.POOL_PATH
}

resource "libvirt_volume" "system_volume" {
  count  = local.SYSTEM_DISK_REFERENCE ? 0 : 1
  name   = "${local.DOMAIN_NAME}-${local.FEDORA_VERSION}-${random_string.suffix.id}"
  size   = local.SYSTEM_DISK_SIZE_IN_BYTE
  format = "raw"
  pool   = libvirt_pool.vdisk.name
}

resource "libvirt_volume" "system_volume_reference" {
  count  = local.SYSTEM_DISK_REFERENCE ? 1 : 0
  name   = "${local.DOMAIN_NAME}-${local.FEDORA_VERSION}-${random_string.suffix.id}"
  source = local.SYSTEM_DISK_REFERENCE_PATH
  pool   = libvirt_pool.vdisk.name
}


resource "libvirt_domain" "workstation_fedora" {
  name     = "${local.DOMAIN_NAME}-${local.FEDORA_VERSION}-${random_string.suffix.id}"
  firmware = "/usr/share/edk2/ovmf/OVMF_CODE.fd"
  vcpu     = 2
  memory   = "4096"
  boot_device {
    dev = ["cdrom", "hd"]
  }
  disk {
    file = local.ISO_DISK
  }

  disk {
    volume_id = local.SYSTEM_DISK_REFERENCE ? libvirt_volume.system_volume_reference[0].id : libvirt_volume.system_volume[0].id
    scsi      = true
  }

  network_interface {
    network_name   = "default"
    wait_for_lease = false
  }
}


