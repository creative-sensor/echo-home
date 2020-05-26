#!/bin/bash



#-------------------------||| IPv6 REGEX PANTHEON |||------------------------------------------------------------------------------------------------------------------
FULL='([A-Fa-f0-9]{1,4}):(([A-Fa-f0-9]{1,4}):){6}([A-Fa-f0-9]{1,4})'
MIDDLE_ZERO_61='(([A-Fa-f0-9]{1,4}:){6}(:[A-Fa-f0-9]{1,4}){1})'
MIDDLE_ZERO_52='(([A-Fa-f0-9]{1,4}:){5}(:[A-Fa-f0-9]{1,4}){1,2})'
MIDDLE_ZERO_43='(([A-Fa-f0-9]{1,4}:){4}(:[A-Fa-f0-9]{1,4}){1,3})'
MIDDLE_ZERO_34='(([A-Fa-f0-9]{1,4}:){3}(:[A-Fa-f0-9]{1,4}){1,4})'
MIDDLE_ZERO_25='(([A-Fa-f0-9]{1,4}:){2}(:[A-Fa-f0-9]{1,4}){1,5})'
MIDDLE_ZERO_16='([A-Fa-f0-9]{1,4}:(:[A-Fa-f0-9]{1,4}){1,6})'
TAIL_ZERO='(([A-Fa-f0-9]{1,4}:){1,6}:)'
LEADING_ZERO='(:(:[A-Fa-f0-9]{1,4}){1,6})'
ALL_ZERO='(::)'

#LONGEST_MATCH_PRIORITY
PATTERN="${FULL}|${MIDDLE_ZERO_61}|${MIDDLE_ZERO_52}|${MIDDLE_ZERO_43}|${MIDDLE_ZERO_34}|${MIDDLE_ZERO_25}|${MIDDLE_ZERO_16}|${TAIL_ZERO}|${LEADING_ZERO}|${ALL_ZERO}"
#----------------------------------------------------------------------------------------------------------------------------------------------------------------------




test "$1" == "-v" && invalid="-v"
grep ${invalid}  --color  -E    "${PATTERN}" test-cases.ipv6
