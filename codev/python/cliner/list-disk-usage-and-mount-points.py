import subprocess
import os

try:
    # Attempt to run 'df -h' to list disk usage and mount points
    result = subprocess.run(['df', '-h'], capture_output=True, text=True, check=True)
    print(result.stdout)
except subprocess.CalledProcessError as e:
    print(f"Error executing df -h: {e.stderr}")
except FileNotFoundError:
    print("Error: 'df' command not found. Cannot list mount points.")
