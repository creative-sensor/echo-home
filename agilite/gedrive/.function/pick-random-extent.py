#!/usr/bin/python
import os,time

LIST_REMOTES = os.environ['LIST_REMOTES']
LIST_REMOTES = LIST_REMOTES.split('\n')
epoch_time = int(time.time())
random_index = epoch_time % len(LIST_REMOTES)
print(LIST_REMOTES[random_index])
