import subprocess
import platform

def check_internet():
    # Determine the ping command based on the OS
    # -c 1 for Linux/macOS, -n 1 for Windows
    param = '-n' if platform.system().lower() == 'windows' else '-c'
    command = ['ping', param, '1', '8.8.8.8']

    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            return "Internet connection is active."
        else:
            return "Internet connection is inactive or unreachable."
    except Exception as e:
        return f"An error occurred while checking connection: {e}"

print(check_internet())
