#!/usr/bin/env python
import os
import qrcode
import qrcode.image.svg

QR_NAME = os.environ['QR_NAME']
ISSUER = os.environ.get('ISSUER', 'Unknown')
KEY = os.environ['KEY']
FULLTEXT = f"otpauth://totp/ID:{QR_NAME}?secret={KEY}&digits=6&issuer={ISSUER}"
datum = f"./data/{QR_NAME}.svg"

factory = qrcode.image.svg.SvgPathFillImage
img = qrcode.make(FULLTEXT, image_factory=factory)
img.save(datum)
print(datum)
