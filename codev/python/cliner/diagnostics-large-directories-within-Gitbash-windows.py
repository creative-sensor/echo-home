import subprocess

try:
    # Attempt to find the top 10 largest directories on the C: drive using du    
    # This is a heuristic to suggest where space might be used.
    result = subprocess.run(['du', '-h', '-d', '1', 'C:/Program Files/Git'], capture_output=True, text=True, check=True)
    print("--- Top 10 largest directories in C:/Program Files/Git ---")
    print(result.stdout)
except subprocess.CalledProcessError as e:
    print(f"Error executing du command: {e.stderr}")
except FileNotFoundError:
    print("Error: 'du' command not found. Cannot analyze disk usage.")

