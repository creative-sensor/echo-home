# ---- GLOBAL ----
import argparse
from datetime import datetime
# ---- GLOBAL.end ----
# ---- FUNCTION ----
def info(str):
    print(f'### {str}')

def showtime():
    print(datetime.now())
# ---- FUNCTION.end ----
# ---- CLASS ----
# ---- CLASS.end ----
# ---- MAIN ---
parser = argparse.ArgumentParser()
parser.add_argument('--date', action='store_true')
args = parser.parse_args()
if args.date:
    showtime()
else:
    info('clock')
    showtime()
# ---- MAIN.end ----
