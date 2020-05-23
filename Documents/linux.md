### move list of file to folder
```bash
find . -type f | grep 'mp4$' |  xargs -I '{}'  mv {}  transfer/
```


###  nmcli , set-ip-address , 
```bash
nmcli c modify eth0  ipv4.method static ipv4.addresses 192.168.1.15/24 ipv4.dns 8.8.8.8 ipv4.gateway 192.168.1.1 connection.autoconnect yes
nmcli con down eth0
nmcli con up eth0
```

###  bond-rr , 
```bash
bond_name =
nic_1     =
nic_2     =
nmcli con add type bond ifname $bond_name
nmcli c modify $bond_name ipv4.method static ipv4.addresses 192.168.1.X/24 ipv4.dns 8.8.8.8 ipv4.gateway 192.168.1.1 connection.autoconnect yes
nmcli con add type ethernet ifname $nic_1 master $bond_name # add slave interface 1 to BOND_RR
nmcli con add type ethernet ifname $nic_2 master $bond_name # add slave interface 2 to BOND_RR
nmcli  c modify  'Interface slave 1' connection.autoconnect no # disable wired profile of NIC1
nmcli  c modify  'Interface slave 2' connection.autoconnect no # disable wired profile of NIC2
nmcli c reload $bond_name
```

###  libvirt , virsh , virsh-shell , snapshot ,
```
snapshot-create --domain NAME
snapshot-list --domain NAME
virsh shutdown --domain NAME
snapshot-revert --domain NAME --snapshotname SNAPSHOT-NAME --running
```


###  libvirt , qemu-img , snapshot ,
```bash
qemu-img snapshot -c SNAPSHOT-NAME /datum/qemu/vdisks/NAMNE
qemu-img info /datum/qemu/vdisks/NAMNE
```


### qemu-image ,
```bash
qemu-img create --formate qcow2 /datum/qemu/vdisks/NAME
qemu-img info /datum/qemu/vdisks/NAME
```


### rsync , 
```bash
rsync --dry-run --archive --verbose --compress -A -X --info=all2 --stats source/ sink/
rsync --dry-run -a -v -z -A -X --info=all2 --stats source/ sink/ #equivalent
```


### raid5 ,
```bash
mdadm --create --verbose /dev/md/NAME --level=5 --raid-devices=3 /dev/vda1 /dev/vdb1 /dev/vdc1 --spare-devices=1 /dev/vdd1
```
### pci-bus-address , block-device , mapping-pci-bus-address-and-block-device ,
```bash
ls  -la /sys/block/
```

### dd-command , dd , dd-by-sector , dd-sector, 
```bash
dd if=/dev/sda bs=4096 count=8200000 | ssh USERNAME@REMOTE-HOST 'cat > /path/to/destination/image'
# Description:
#  4096 = 512*8        //512 byte sector
#  8200000 = 65600000 / 8        // 65600000 sectors
```



###  sudo-without-password ,
```bash
username="ubuntu" ; test -f "/etc/sudoers.d/$username" || echo "$username ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$username
```


### test-tcp-port-availability ,
```bash
ip=
port=
nc -zv  $ip $port
```



### sed, no-escaping-slash ,  
```bash
#using another delimiter to prevent you from escaping slash:
sed 's,/,\\/,g'
```



### yaml-update ,
```
cat bitbucket-pipelines.yml |  yq   '.pipelines.branches."build-initial-ci"[1].step.script = { "helikon-tex" : "no1", "x" : [{"e1" : "00"} , "e2"]}'
```

### change-root-password-forgotten
```
1. Boot console: edit grub entry (press e)
2. Grub entry:
   - replace  ro  with  rw  in linux,linux16,linuxefi
   - append in linux,linux16,linuxefi
        rw init=/bin/bash

        or (disk encrypted)
        rw init=/bin/bash plymouth.enable=0
3. Bash up and running:
    passwd
4. Relabel:
    touch /.autorelabel
5. Reboot:
    /sbin/reboot -f
```


