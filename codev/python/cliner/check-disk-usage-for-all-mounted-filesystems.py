import os

# Check disk usage for all mounted filesystems
command = "df -h"
output = os.popen(command).read()
print(output)
