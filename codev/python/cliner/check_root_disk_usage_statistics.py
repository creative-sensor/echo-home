import shutil

def check_disk_usage():
    # Get disk usage for the root directory
    # On Windows, this is usually 'C:\', on Unix it's '/'
    # shutil.disk_usage works for both.
    total, used, free = shutil.disk_usage("/")

    print(f"Total: {total // (2**30)} GB")
    print(f"Used: {used // (2**30)} GB")
    print(f"Free: {free // (2**30)} GB")
    print(f"Percentage Used: {(used / total) * 100:.2f}%")

check_disk_usage()
