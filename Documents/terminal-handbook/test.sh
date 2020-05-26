PATTERN_v64='([A-Fa-f0-9]{1,4})?:(([A-Fa-f0-9]{1,4})?:){1,5}${OCTET}\.${OCTET}\.${OCTET}\.${OCTET}'


FULL='([A-Fa-f0-9]{1,4}):(([A-Fa-f0-9]{1,4})?:){6}([A-Fa-f0-9]{1,4})'
TAIL_ZERO='(([A-Fa-f0-9]{1,4}:){1,6}:)'
LEADING_ZERO='(:(:[A-Fa-f0-9]{1,4}){1,6})'
MIDDLE_ZERO_RIGHT='(([A-Fa-f0-9]{1,4}:){1,6}:[A-Fa-f0-9]{1,4})'
MIDDLE_ZERO_LEFT='([A-Fa-f0-9]{1,4}:(:[A-Fa-f0-9]{1,4}){1,6})'
MIDDLE_ZERO_52='(([A-Fa-f0-9]{1,4}:){2,5}(:[A-Fa-f0-9]{1,4}){2})'
MIDDLE_ZERO_25='(([A-Fa-f0-9]{1,4}:){2}(:[A-Fa-f0-9]{1,4}){2,5})'
MIDDLE_ZERO_34='(([A-Fa-f0-9]{1,4}:){3}(:[A-Fa-f0-9]{1,4}){2,4})'
MIDDLE_ZERO_43='(([A-Fa-f0-9]{1,4}:){2,4}(:[A-Fa-f0-9]{1,4}){3})'
ALL_ZERO='::'

STANDARD_PATTERN_v6="${FULL}|${TAIL_ZERO}|${LEADING_ZERO}|${MIDDLE_ZERO_RIGHT}|${MIDDLE_ZERO_LEFT}|${MIDDLE_ZERO_52}|${MIDDLE_ZERO_25}|${MIDDLE_ZERO_34}|${MIDDLE_ZERO_43}"
IPv6=
grep --color -E  "${STANDARD_PATTERN_v6}" test-cases
