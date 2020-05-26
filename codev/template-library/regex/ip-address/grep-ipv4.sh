

#------------------ IPv4 REGEX PANTHEON -----------------
#_V4_
#              [250 -> 255]
#              |           [200 -> 249]
#              |           |               [0 -> 199]
#              |           |               |
OCTET='(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
PATTERN="${OCTET}\.${OCTET}\.${OCTET}\.${OCTET}"
#--------------------------------------------------------




test "$1" == "-v" && invalid="-v"
grep ${invalid}  --color -E  "${PATTERN}"    test-cases.ipv4
