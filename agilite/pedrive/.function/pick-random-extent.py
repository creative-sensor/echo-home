#!/usr/bin/env python
import os,time

LIST_EXTENTS = os.environ['LIST_EXTENTS']
LIST_EXTENTS = LIST_EXTENTS.split('\n')
epoch_time = int(time.time())
random_index = epoch_time % len(LIST_EXTENTS)
print(LIST_EXTENTS[random_index])
