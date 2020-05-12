#!/bin/bash -e

checksum=https://releases.hashicorp.com/terraform/0.12.24/terraform_0.12.24_SHA256SUMS
bin=https://releases.hashicorp.com/terraform/0.12.24/terraform_0.12.24_linux_amd64.zip
sig=https://releases.hashicorp.com/terraform/0.12.24/terraform_0.12.24_SHA256SUMS.sig

# This is the public key from above - one-time step.
gpg2 --import tf-pubkey.asc

# Download the binary and signature files.
curl -Os ${bin}
curl -Os ${checksum}
curl -Os ${sig} 

print_filename(){
    awk -F "/" '{print  $NF}'
}
checksum=$(echo $checksum | print_filename)
bin=$(echo $bin | print_filename)
sig=$(echo $sig | print_filename)

# Verify the signature file is untampered.
gpg2 --verify $sig  $checksum

# Verify the SHASUM matches the binary.
sha256sum -c $checksum  | grep --color "${bin}.*OK"

unzip $bin
mv -b terraform ~/bin/
