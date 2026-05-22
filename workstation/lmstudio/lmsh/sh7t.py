import os
import json
import subprocess
import re
import argparse
from openai import OpenAI
import requests
from typing import Optional

import subprocess
import signal
from typing import Dict


parser = argparse.ArgumentParser(description="Connect to a local OpenAI API endpoint.")
parser.add_argument(
    '--port', 
    type=int, 
    default=8080, 
    help='The port number of the local LLM server'
)
parser.add_argument(
    '--host', 
    type=str, 
    default='localhost', 
    help='The hostname of the local LLM server'
)
args = parser.parse_args()
port = args.port
host = args.host

# Configure for local LLM (e.g., Ollama, vLLM, or LM Studio)
client = OpenAI(
    base_url=f"http://{host}:{port}/v1", 
    api_key="localm" 
)


def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        # 1. Execute the GET request (replaces 'curl')
        response = requests.get(url, timeout=10)
        
        # 2. Check for HTTP errors (4xx or 5xx status codes)
        response.raise_for_status() 
        
        # 3. Parse the JSON body (replaces the structure of the data)
        data = response.json()
        
        # 4. Extract the name of the first model (replaces '.models[0].name')
        # We use structured indexing to prevent KeyErrors if the API structure changes.
        if 'models' in data and data['models']:
            first_model = data['models'][0]
            if 'name' in first_model:
                model_name = first_model['name']
                print(f"✅ Ready: {model_name}")
                return model_name
            else:
                print("⚠️ Warning: Model structure found, but 'name' key is missing.")
                return None
        else:
            print("⚠️ Warning: API response was empty or missing the 'models' list.")
            return None

    except requests.exceptions.ConnectionError:
        print("\n❌ ERROR: Connection Error.")
        print("   Ensure that the API service is running and accessible at the specified host and port.")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"\n❌ ERROR: HTTP Error occurred: {e}")
        print("   Check if the endpoint '/models' is correct and the server supports it.")
        return None
    except requests.exceptions.Timeout:
        print("\n❌ ERROR: The request timed out.")
        return None
    except json.JSONDecodeError:
        print("\n❌ ERROR: Failed to decode JSON. The API might be returning non-JSON data.")
        return None
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
        return None

# --- Execution ---

# 1. Get the model name programmatically
MODEL_NAME = model_name(
    host, 
    port, 
    endpoint="/models"
)


SYSTEM_PROMPT = """You are an autonomous shell operator. You have access to a terminal. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

RULES:
1. You operate in a loop. You can either issue a shell command to execute/verify something, OR you can declare the task finished.
2. Do not declare SUCCESS unless you have explicit proof from stdout/stderr or by running a verification command (e.g., using `ls`, `cat`, or `docker ps`).
3. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_command" or "declare_result",
  "command": "<shell command> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}
"""



def execute_shell_command(command: str) -> Dict:
    """
    Executes a shell command. Uses subprocess.Popen to allow manual 
    management of the process and guarantee forceful termination 
    (SIGKILL) if the timeout is reached.
    """
    print(f"\n[SYSTEM] Executing: {command}")
    
    # Use the command list for Popen to avoid shell injection risk 
    # (even though the original used shell=True, Popen is safer this way)
    cmd_args = ["bash", "-c", command]
    
    proc = None
    git_bash_path = r"C:\Program Files\Git\bin\bash.exe"
    try:
        # Start the process
        proc = subprocess.Popen(
            cmd_args, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True,
            executable=git_bash_path
        )
        
        try:
            # Wait for the process, with a specified timeout
            stdout_data, stderr_data = proc.communicate(timeout=30)
            
            # If communicate succeeds, the process finished normally
            return {
                "exit_code": proc.returncode,
                "stdout": stdout_data.strip(),
                "stderr": stderr_data.strip()
            }

        except subprocess.TimeoutExpired:
            # --- THIS IS THE KEY CHANGE ---
            # 1. The process is still running.
            # 2. We explicitly send the SIGKILL signal (Signal 9).
            print(f"[SYSTEM] Timeout reached (30s). Forcing termination (SIGKILL).")
            proc.kill() 
            
            # Wait a moment for the OS to finalize the kill
            proc.wait() 
            
            # Return the timeout status
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out and was forcefully terminated (SIGKILL)."}

    except FileNotFoundError:
        return {"exit_code": 127, "stdout": "", "stderr": "Bash interpreter not found."}
    except Exception as e:
        # General error handling
        return {"exit_code": 1, "stdout": "", "stderr": f"An unexpected error occurred: {str(e)}"}
    finally:
        # Ensure the process handle is cleaned up if it was started but failed before communicate()
        if proc and proc.poll() is None:
             proc.kill()




#def execute_shell_command(command: str) -> dict:
#    """Executes a shell command and returns the stdout, stderr, and exit code."""
#    print(f"\n[SYSTEM] Executing: {command}")
#    try:
#        # WARNING: shell=True is dangerous. Run in an isolated environment.
#        result = subprocess.run(
#            command, 
#            shell=True, 
#            capture_output=True, 
#            text=True, 
#            timeout=30,
#        )
#        return {
#            "exit_code": result.returncode,
#            "stdout": result.stdout.strip(),
#            "stderr": result.stderr.strip()
#        }
#    except subprocess.TimeoutExpired:
#        return {"exit_code": 124, "stdout": "", "stderr": "Command timed out."}
#    except Exception as e:
#        return {"exit_code": 1, "stdout": "", "stderr": str(e)}

def parse_llm_json(raw_text: str) -> dict:
    """Cleans up potential markdown from the LLM and parses the JSON."""
    clean_text = re.sub(r"```json", "", raw_text)
    clean_text = re.sub(r"```", "", clean_text).strip()
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        print(f"[ERROR] Failed to parse JSON from LLM: {raw_text}")
        return {"action_type": "declare_result", "status": "FAILED", "reason": "LLM output invalid JSON."}

def run_agent_loop(user_intent: str, max_turns: int = 7):
    """Manages the multi-turn execution and validation loop."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"User Intent: {user_intent}"}
    ]

    for turn in range(max_turns):
        print(f"\n\033[36m\033[1m---- Turn {turn + 1} ----\033[0m")
#        print(f"\n---- Turn {turn + 1} ----")
        
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        llm_output = response.choices[0].message.content
        parsed_action = parse_llm_json(llm_output)
        
        messages.append({"role": "assistant", "content": json.dumps(parsed_action)})

        action_type = parsed_action.get("action_type")
        
        if action_type == "declare_result":
            status = parsed_action.get("status")
            reason = parsed_action.get("reason")
            print(f"\n❇  [FINAL RESULT] {status}")
            print(f"🧠 [REASON] {reason}")
            return
            
        elif action_type == "run_command":
            command = parsed_action.get("command")
            if not command:
                print("\n❌ [ERROR] LLM requested run_command but provided no command.")
                break
                
            # Execute the command
            exec_result = execute_shell_command(command)
            
            # --- NEW DISPLAY LOGIC: Show standard output/error to the user ---
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and not exec_result['stderr']:
                print("[OUTPUT] (No standard output or error)")
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            # -----------------------------------------------------------------
            
            # Format the system evidence for the LLM
            evidence = (
                f"Command Executed: {command}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            
            # Feed the evidence back to the LLM for the next turn
            messages.append({"role": "user", "content": evidence})
            
        else:
            print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

    print("\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.")

if __name__ == "__main__":
    print("🚀 Local LLM CLI Agent Initialized. Type 'exit' to quit.")
    while True:
        try:
            COLOR_BG='\033[48;5;11m'
            COLOR_BG2='\033[48;5;234m'
            COLOR_FG='\033[1;34m\033[38;5;234m'
            COLOR_FG2='\033[38;5;11m'
            STOP='\033[0m'

            user_input = input(f"\n{COLOR_BG}{COLOR_FG} LLM {STOP}{COLOR_BG2}{COLOR_FG2} Prompt!a {STOP} ")
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_agent_loop(user_input)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
