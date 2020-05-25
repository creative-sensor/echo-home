### ipv4 , ipv6
THESE REGEX ONLY RECOGNIZE THE PATTERN AND TAKE NO CONTEXT INTO ACCOUNT
```bash
#GREP

#_V4_
#              [250 -> 255]
#              |           [200 -> 249]
#              |           |               [0 -> 199]
#              |           |               |
OCTET='(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
IP=_111.225.233.0000-
echo ${IP} | grep -E "${OCTET}\.${OCTET}\.${OCTET}\.${OCTET}" 



#_V6_
#                           1st-octet [0 -> FFFF/ffff]
#                           |                          6 nested-octets (":"   or   "[0: -> FFFF/ffff:]"
#                           |                          |
#                           |                          |                  last-octet
#                           |                          |                  |
PATTERN_v6='[A-Fa-f0-9]{1,4}:(([A-Fa-f0-9]{1,4})?:){0,6}([A-Fa-f0-9]{1,4})?'
IPv6=
echo ${IPv6} | grep -E  "${PATTERN_v6}"
```

```bash
#SED
```

