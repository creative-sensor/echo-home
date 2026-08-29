#!/usr/bin/env python

# ---- GLOBAL ----
import shutil
# ---- GLOBAL.end ----

# ---- DEFINITIONS (Classes & Functions) ----
def check_disk_usage():
    total, used, free = shutil.disk_usage('/')
    total_gb = total // 2 ** 30
    used_gb = used // 2 ** 30
    free_gb = free // 2 ** 30
    percentage = used / total * 100
    bar_length = 20
    filled = int(percentage / 5)
    bar = '\x1b[32m' + '#' * filled + '\x1b[36m' + '-' * (bar_length - filled) + '\x1b[0m'
    print(f'Disk Usage: [{bar}] {percentage:.1f}%')
    print(f"{'Total:':<10} {total_gb:>5} GB")
    print(f"{'Used:':<10} {used_gb:>5} GB")
    print(f"{'Free:':<10} {free_gb:>5} GB")

# ---- DEFINITIONS.end ----

# ---- MAIN ---
check_disk_usage()
# ---- MAIN.end ----
