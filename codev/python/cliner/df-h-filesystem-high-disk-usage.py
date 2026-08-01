import subprocess

# Run df -h again to get fresh data
result = subprocess.run(['df', '-h'], capture_output=True, text=True, check=True)
output = result.stdout

lines = output.strip().split('\n')
# Skip header
lines.pop(0)

high_usage_entries = []

for line in lines:
    parts = line.split() # Split by whitespace
    if len(parts) >= 6:
        filesystem = parts[0]
        size = parts[1]
        used = parts[2]
        avail = parts[3]
        capacity = parts[4] # This is the %Used
        mounted_on = parts[-1]
        
        try:
            # Extract percentage from capacity (e.g., '100%' -> 100)
            usage_percent_str = capacity.replace('%', '') 
            usage_percent = int(usage_percent_str)
            
            if usage_percent >= 90:
                high_usage_entries.append({
                    'Filesystem': filesystem,
                    'Size': size,
                    'Used': used,
                    'Avail': avail,
                    'Capacity': capacity,
                    'Mounted_on': mounted_on
                })
        except ValueError:
            # Skip lines where percentage extraction fails
            continue

if high_usage_entries:
    print("High Disk Usage Detected:")
    for entry in high_usage_entries:
        print(f"  Filesystem: {entry['Filesystem']} -> Mounted on: {entry['Mounted_on']} | Usage: {entry['Capacity']}")
else:
    print("No disk usage above 90% detected.")
