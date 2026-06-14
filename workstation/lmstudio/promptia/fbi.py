#!/usr/bin/env python
import os
import json
import re
import argparse
import sys
import subprocess
import threading
from openai import OpenAI
import requests
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

parser = argparse.ArgumentParser(description="Investigate agent workspace failures.")
parser.add_argument('--workspace', type=str, required=True, help='Path to the failed workspace folder')
parser.add_argument('--log', type=str, default=None, help='Path to the console log file. Defaults to /workspace/console.lmsh')
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
args = parser.parse_args()

client = OpenAI(base_url=f"http://{args.host}:{args.port}/v1", api_key="localm")

# --- System Configuration ---
WORKSPACE_DIR = os.path.abspath(args.workspace)

if not os.path.exists(WORKSPACE_DIR):
    print(f"❌ ERROR: Workspace directory '{WORKSPACE_DIR}' does not exist.")
    sys.exit(1)

print_lock = threading.Lock() 

def safe_print(*args, **kwargs):
    """Thread-safe printing."""
    with print_lock:
        print(*args, **kwargs)

def agent_print(step_name: str, message: str, color: str = ""):
    """Helper to print agent logs with indentation."""
    reset = "\033[0m"
    prefix = f"    │ \033[90m[{step_name}]\033[0m "
    
    formatted_lines = []
    for line in str(message).splitlines():
        formatted_lines.append(f"{prefix}{color}{line}{reset}")
        
    safe_print("\n".join(formatted_lines))

def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if 'models' in data and data['models']:
            return data['models'][0].get('name')
    except Exception as e:
        safe_print(f"\n❌ ERROR connecting to LLM: {e}")
    return None

def check_docker():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        safe_print("❌ ERROR: Docker is not running or not installed. Please start Docker to continue.")
        sys.exit(1)

# --- Execution ---

MODEL_NAME = model_name(args.host, args.port, endpoint="/models")
if MODEL_NAME:
    safe_print(f"✅ Ready: {MODEL_NAME}")

# ==========================================
# SYSTEM PROMPTS
# ==========================================

INVESTIGATOR_SYSTEM_PROMPT = """You are an expert Investigator Agent. A previous autonomous workflow has failed. Your goal is to analyze the console logs and actively inspect the generated files to determine the exact root cause of the failure.

ENVIRONMENT:
1. A persistent host volume of the failed workflow is mounted at `/workspace`.
2. You can run any `image` (e.g., 'ubuntu', 'python:3.11-slim', 'jq') to inspect this volume.
3. Use commands like `cat`, `ls -la`, `grep`, or run test scripts to examine files or check partial outputs.

WORKFLOW:
1. You will be provided with initial context (directory listing and recent log tails).
2. Loop using `run_container` to actively explore `/workspace` to figure out what went wrong.
3. Once you thoroughly understand the failure, use `declare_result` to output your analysis and provide a NEW PROMPT that the human user can use to retry and fix the workflow.

OUTPUT SCHEMA:
{
  "action_type": "run_container" or "declare_result",
  "image": "<docker image> or null",
  "command": "<command to run, e.g., 'cat /workspace/error.log'> or null",
  "analysis": "<String detailing the root cause. ONLY use this with declare_result> or null",
  "suggested_prompt": "<The exact, robust prompt the user should paste into the agent loop to resume/fix the workflow. ONLY use this with declare_result> or null"
}
"""

# ==========================================
# AGENT LOGIC
# ==========================================

def parse_llm_json(raw_text: str) -> dict:
    clean_text = re.sub(r"```json", "", raw_text)
    clean_text = re.sub(r"```", "", clean_text).strip()
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        safe_print(f"[ERROR] Failed to parse JSON:\n{raw_text}")
        return {"action_type": "declare_result", "analysis": "Failed to parse JSON output.", "suggested_prompt": None}

def execute_docker_task(agent_name: str, task: dict) -> dict:
    image = task.get("image", "ubuntu")
    if not image:
        image = "ubuntu"
        
    command_str = task.get("command", "")

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{WORKSPACE_DIR}:/workspace",
        "-w", "/workspace",
        "-e", "DOCKER_HOST=tcp://host.docker.internal:2375",
        image,
        "sh", "-c", command_str
    ]

    agent_print(agent_name, f"🐳 Running {image} -> {command_str}", color="\033[33m") 
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.stdout:
            agent_print(agent_name, f"[STDOUT]\n{res.stdout.strip()}", color="\033[90m") 
        if res.stderr:
            agent_print(agent_name, f"[STDERR]\n{res.stderr.strip()}", color="\033[31m") 
            
        return {
            "exit_code": res.returncode,
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip()
        }
    except Exception as e:
         agent_print(agent_name, f"❌ Execution Exception: {str(e)}", color="\033[31m")
         return {"exit_code": 1, "stdout": "", "stderr": str(e)}

def get_initial_context() -> str:
    """Gathers directory structure and recent logs to jumpstart the LLM."""
    context = "WORKSPACE INITIAL CONTEXT:\n\n"
    
    # 1. Directory Listing
    try:
        files = os.listdir(WORKSPACE_DIR)
        context += f"Files in /workspace: {', '.join(files)}\n\n"
    except Exception as e:
        context += f"Could not list directory: {e}\n\n"

    # 2. Extract Console Log Tail
    log_file_path = args.log if args.log else os.path.join(WORKSPACE_DIR, 'console.lmsh')
    
    if os.path.exists(log_file_path):
        try:
            with open(log_file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Grab last 200 lines to save context
                tail = "".join(lines[-200:])
                context += f"--- TAIL OF {os.path.basename(log_file_path)} ---\n{tail}\n\n"
        except Exception as e:
            context += f"Could not read log file {log_file_path}: {e}\n\n"
    else:
         context += f"WARNING: Target log file {log_file_path} not found.\n\n"

    return context

def run_investigator_loop(user_context: str = "", max_turns: int = 55):
    """Loop specialized in debugging failures using the workspace history."""
    
    initial_evidence = get_initial_context()
    if user_context:
        initial_evidence += f"\nUSER NOTES: {user_context}"

    messages = [
        {"role": "system", "content": INVESTIGATOR_SYSTEM_PROMPT},
        {"role": "user", "content": f"Please investigate the failure in the mounted `/workspace`.\n\n{initial_evidence}"}
    ]

    safe_print(f"\n\033[35m\033[1m==== 🕵️‍♂️ Investigator Loop Initialized ====\033[0m")
    
    for turn in range(max_turns):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        parsed_action = parse_llm_json(response.choices[0].message.content)
        messages.append({"role": "assistant", "content": json.dumps(parsed_action)})

        action_type = parsed_action.get("action_type")
        
        if action_type == "declare_result":
            analysis = parsed_action.get('analysis', 'No analysis provided.')
            suggested_prompt = parsed_action.get('suggested_prompt', 'No prompt suggested.')
            
            safe_print("\n" + "="*50)
            safe_print(f"🕵️‍♂️ \033[1m[ROOT CAUSE ANALYSIS]\033[0m\n{analysis}")
            safe_print("-" * 50)
            safe_print(f"💡 \033[1m[SUGGESTED NEXT PROMPT]\033[0m\n\033[32m{suggested_prompt}\033[0m")
            safe_print("="*50 + "\n")
            return
            
        elif action_type == "run_container":
            safe_print(f"🕵️‍♂️ Turn {turn + 1}: Examining workspace...")
            exec_result = execute_docker_task("Investigator", parsed_action)
            
            evidence = (
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            messages.append({"role": "user", "content": evidence})
            
        else:
            safe_print(f"\n❌ [ERROR] Unknown investigator action: {action_type}")
            break

    safe_print("\n⚠️ [WARNING] Max investigator turns reached.")

if __name__ == "__main__":
    check_docker()
    safe_print(f"🚀 Investigator Agent Initialized.")
    safe_print(f"📁 Target Workspace: {WORKSPACE_DIR}")
    
    log_target = args.log if args.log else f"{WORKSPACE_DIR}/console.lmsh"
    safe_print(f"📄 Targeting Log: {log_target}")
    
    prompt_session = PromptSession()
    cli_style = Style.from_dict({
        'llm': 'bg:#005f87 fg:#000000 bold',   
        'prompt': 'bg:#000000 fg:#005f87',     
        'ws': 'bg:#005f87 fg:#005f87'          
    })
    
    try:
        while True:
            user_input = prompt_session.prompt(
                    [('class:llm', ' FBI '), ('class:prompt', ' Guidance (or Enter to auto-run) '), ('class:ws', ' ')],
                    multiline=False,
                    style=cli_style
            )
            
            clean_input = user_input.strip().lower()
            if clean_input in ['exit', 'quit']:
                break
            
            run_investigator_loop(user_context=user_input)
                
    except KeyboardInterrupt:
        safe_print("\nExiting...")
