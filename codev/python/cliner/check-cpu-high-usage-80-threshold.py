import psutil
import time

def check_cpu_usage():
    try:
        # Get CPU usage percentage
        cpu_percent = psutil.cpu_percent(interval=1)
        print(f"CPU Usage: {cpu_percent}%")
        if cpu_percent > 80:
            print("Status: High CPU usage detected.")
        else:
            print("Status: CPU usage is normal.")
    except Exception as e:
        print(f"Error checking CPU usage: {e}")
        print("Note: psutil library might not be installed or system access denied.")

check_cpu_usage()
