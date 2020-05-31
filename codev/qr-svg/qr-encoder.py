#!/usr/bin/env python

import INPUT_SET
import qrcode
import qrcode.image.svg

datum = "./data/" + INPUT_SET.QR_NAME + ".svg"

factory = qrcode.image.svg.SvgPathFillImage
img = qrcode.make(INPUT_SET.DATA, image_factory=factory)
img.save(datum)
print(datum)
