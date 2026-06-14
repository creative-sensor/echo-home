import subprocess
import re

def check_zombie_processes():
    # Command to list processes, including PID, Status, and Command
    # We look for processes with status 'Z' (zombie)
    try:
        # Use ps command to get process information. 
        # -o pid,stat: output only PID and Status
        # We filter for processes where the status starts with 'Z'
        result = subprocess.run(['ps', '-eo', 'pid,stat'], capture_output=True, text=True, check=True)
        output = result.stdout
    except FileNotFoundError:
        print("Error: 'ps' command not found. Cannot check processes.")
        return
    except subprocess.CalledProcessError as e:
        print(f"Error executing ps command: {e.stderr}")
        return

    zombie_pids = []
    # Regex to capture PID and Status (e.g., 1234 Z)
    # We look for lines where the status column starts with Z
    # The exact format depends on the OS/ps version, but 'Z' is standard for zombie.
    # We assume the output format is standard: PID STAT COMMAND
    # We look for lines where the second column (STAT) starts with Z
    # A simpler approach is to check the status column directly.
    
    # Split output into lines and iterate
    lines = output.strip().split('\n')
    
    # Skip header if present (assuming standard ps output)
    if lines and 'PID' in lines[0] or 'STAT' in lines[0]:
        lines = lines[1:]

    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            pid = parts[0]
            status = parts[1]
            if status.startswith('Z'):
                zombie_pids.append(pid)

    if zombie_pids:
        print(f"Found zombie processes (Status starting with Z): {zombie_pids}")
    else:
        print("No zombie processes found.")

check_zombie_processes()
