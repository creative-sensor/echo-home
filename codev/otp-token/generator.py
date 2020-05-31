#!/usr/bin/env python

import INPUT_SET
import pyotp

INPUT_SET.SECRET_KEY = INPUT_SET.SECRET_KEY.replace(" ", "")
totp = pyotp.TOTP(INPUT_SET.SECRET_KEY)
print(totp.now())


