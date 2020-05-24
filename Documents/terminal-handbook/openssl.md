### print-certificate-from-server , debug-ssl-connection ,
```bash
    host=google.com
    port=443
    openssl s_client -connect   $host:$port  -showcerts

    #OR
    host=
    port=
    servername=
    openssl s_client  -servername $servername -connect $host:$port 2>/dev/null
```

### print-certificate-from-file , 
```bash
   pem_file=
   openssl x509  -text  -in $pem_file 
   #OR
   openssl x509  -text -noout -in $pem_file 
```

### print-certificate-field , 
```bash
   pem_file=
   openssl x509  -noout  -in $pem_file  -fingerprint
   openssl x509  -noout  -in $pem_file  -serial
   openssl x509  -noout  -in $pem_file  -subject
   openssl x509  -noout  -in $pem_file  -dates
```

### verify key and certificate ,
```bash
   cert_file=
   key_file=
   openssl x509 -noout -modulus -in $cert_file | sha1sum -
   openssl rsa -noout -modulus -in $key_file   | sha1sum -
```

### generate-csr ,
```bash
    openssl req -new > file.csr
```

### generate-self-signed-certificate ,
```bash
    cert_file=my.crt
    key_file=my.key
    openssl req -new -newkey rsa:4096 -x509 -sha256 -days 365 -nodes -out $cert_file -keyout $key_file
```

### generate-ca-key-and-certificate ,
```bash
    key=
    cert=
    openssl req -x509 -days 365 -newkey rsa:2048 -keyout $key -out $cert
```

### verify-certificate-issued-by-ca ,
```bash
    ca_file=
    cert_file=
    openssl verify -verbose -CAfile $ca_file  $cert_file
```

### convert-x509-certificate-to-csr ,
```bash
    cert_file=
    sign_key=
    csr_file=out.csr
    openssl x509 -x509toreq -in $cert_file  -signkey $sign_key      -out $csr_file
```
