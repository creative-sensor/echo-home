#!/usr/bin/env python
import os
import json
import subprocess
import re
import argparse
import platform
from typing import Optional, Dict
import requests
from openai import OpenAI

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style


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
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        
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


def get_os_context() -> str:
    """Detects the operating system, release version, and shell environment."""
    os_system = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    
    context = f"OS: {os_system} {os_release} (Architecture: {arch})"
    
    if os.environ.get('OS') == 'Windows_NT' or os_system == 'Windows':
        context += " | Executing via Git Bash on Windows"
    elif os_system == 'Darwin':
        context += " | Executing via macOS Terminal"
    elif os_system == 'Linux':
        context += " | Executing via Linux bash shell"
        
    return context


# --- Execution ---

# 1. Get the model name programmatically
MODEL_NAME = model_name(
    host, 
    port, 
    endpoint="/models"
)

# 2. Updated System Prompt with dynamic OS environment placeholder
SYSTEM_PROMPT = """You are an autonomous shell operator. You have access to a terminal. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

CURRENT ENVIRONMENT:
{os_context}

RULES:
1. You operate in a loop. You can either issue a shell command tailored to the CURRENT ENVIRONMENT to execute/verify something, OR you can declare the task finished.
2. Do not declare SUCCESS unless you have explicit proof from stdout/stderr or by running a verification command appropriate for your environment.
3. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{{
  "action_type": "run_command" or "declare_result",
  "command": "<shell command> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}}
"""


def execute_shell_command(command: str) -> Dict:
    """
    Executes a shell command. Uses subprocess.Popen to allow manual 
    management of the process and guarantee forceful termination 
    (SIGKILL) if the timeout is reached.
    """
    print(f"\n[SYSTEM] Executing: {command}")
    
    cmd_args = ["bash", "-c", command]
    
    proc = None
    execbin = None
    if os.environ.get('OS') == 'Windows_NT':
        execbin = r"C:\Program Files\Git\bin\bash.exe"     
    try:
        proc = subprocess.Popen(
            cmd_args, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True,
            executable=execbin
        )
        
        try:
            stdout_data, stderr_data = proc.communicate(timeout=30)
            
            return {
                "exit_code": proc.returncode,
                "stdout": stdout_data.strip(),
                "stderr": stderr_data.strip()
            }

        except subprocess.TimeoutExpired:
            print(f"[SYSTEM] Timeout reached (30s). Forcing termination (SIGKILL).")
            proc.kill() 
            proc.wait() 
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out and was forcefully terminated (SIGKILL)."}

    except FileNotFoundError:
        return {"exit_code": 127, "stdout": "", "stderr": "Bash interpreter not found."}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"An unexpected error occurred: {str(e)}"}
    finally:
        if proc and proc.poll() is None:
             proc.kill()


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
    # Inject dynamic OS context into the system prompt
    formatted_system_prompt = SYSTEM_PROMPT.format(os_context=get_os_context())
    
    messages = [
        {"role": "system", "content": formatted_system_prompt},
        {"role": "user", "content": f"User Intent: {user_intent}"}
    ]

    for turn in range(max_turns):
        print(f"\n\033[36m\033[1m---- Turn {turn + 1} ----\033[0m")
        
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
                
            exec_result = execute_shell_command(command)
            
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and not exec_result['stderr']:
                print("[OUTPUT] (No standard output or error)")
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            
            evidence = (
                f"Command Executed: {command}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            
            messages.append({"role": "user", "content": evidence})
            
        else:
            print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

    print("\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.")


if __name__ == "__main__":
    print("🚀 Local LLM CLI Agent Initialized. Type 'exit' to quit.")
    print(f"🖥️  Detected System: {get_os_context()}")
    while True:
        try:
            promptia_session = PromptSession()
            promptia_style = Style.from_dict({
                'llm': 'bg:#c4c408 fg:#000000 bold',
                'prompt': 'bg:#000000 fg:#c4c408',  
                'ws': 'bg:#c4c408 fg:#c4c408'       
            })
            user_input = promptia_session.prompt(
                    [('class:llm', ' SH7T '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=promptia_style
            )
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_agent_loop(user_input)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
