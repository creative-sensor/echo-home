#!/usr/bin/env python

# ---- GLOBAL ----
import subprocess
# ---- GLOBAL.end ----

# ---- DEFINITIONS (Classes & Functions) ----
def test():
    exit

# ---- DEFINITIONS.end ----

# ---- MAIN ---
try:
    result = subprocess.run(['du', '-h', '-d', '1', 'C:/Program Files/Git'], capture_output=True, text=True, check=True)
    lines = result.stdout.strip().split('\n')
    max_dir_width = len('Top-Level Directory')
    max_size_width = len('Size')
    parsed_entries = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            size = parts[0]
            directory_name = ' '.join(parts[1:])
            parsed_entries.append((size, directory_name))
            max_dir_width = max(max_dir_width, len(directory_name))
            max_size_width = max(max_size_width, len(size))
    dir_width = max_dir_width + 2
    size_width = max_size_width + 2
    markdown_table = []
    markdown_table.append(f"| {'Top-Level Directory':<{dir_width}} | {'Size':<{size_width}} |")
    markdown_table.append(f"| {'-' * dir_width} | {'-' * size_width} |")
    for size, directory_name in parsed_entries:
        markdown_table.append(f'| {directory_name:<{dir_width}} | {size:<{size_width}} |')
    print('\n'.join(markdown_table))
except subprocess.CalledProcessError as e:
    print(f'Error executing du command: {e.stderr}')
except FileNotFoundError:
    print("Error: 'du' command not found. Cannot analyze disk usage.")
# ---- MAIN.end ----
