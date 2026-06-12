import psutil

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

def check_ram_usage():
    try:
        # Get virtual memory statistics
        memory = psutil.virtual_memory()
        print("--- RAM Usage ---")
        print(f"Total Memory: {memory.total / (1024**3):.2f} GB")
        print(f"Used Memory: {memory.used / (1024**3):.2f} GB")
        print(f"Available Memory: {memory.available / (1024**3):.2f} GB")
        print(f"Memory Usage Percentage: {memory.percent}%")
    except Exception as e:
        print(f"Error checking RAM usage: {e}")
        print("Note: psutil library might not be installed or system access denied.")

# Execute both checks
check_cpu_usage()
check_ram_usage()
