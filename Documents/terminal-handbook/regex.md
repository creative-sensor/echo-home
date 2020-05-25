### ipv4 , ipv6
THESE REGEX ONLY RECOGNIZE THE PATTERN AND TAKE NO CONTEXT INTO ACCOUNT
```bash
#GREP - IP=_111.225.233.0000-
OCTET='\(25[0-5]\|2[0-4][0-9]\|[01]\?[0-9][0-9]\?\)'
echo ${IP} | grep  "${OCTET}\.${OCTET}\.${OCTET}\.${OCTET}" 
```

```bash
#SED
```

