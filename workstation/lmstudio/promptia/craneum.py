#!/usr/bin/env python
import os
import json
import re
import argparse
import sys
import subprocess
import shlex
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
import requests
from typing import Optional, Dict, List

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

parser = argparse.ArgumentParser(description="Connect to a local OpenAI API endpoint.")
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
parser.add_argument('--plan', type=str, default=None, help='Optional plan name to use as the workspace subdirectory')
args = parser.parse_args()

client = OpenAI(base_url=f"http://{args.host}:{args.port}/v1", api_key="localm")

# --- System Configuration ---
# Determine the workspace subdirectory based on the --plan argument or current time
if args.plan:
    workspace_subdir = args.plan
else:
    now = datetime.now()
    seconds_since_midnight = (now.hour * 3600) + (now.minute * 60) + now.second
    # Pad the seconds to 5 digits (e.g., 00045, 86399) for clean chronological sorting
    workspace_subdir = f"{now.strftime('%Y-%m-%d')}-{seconds_since_midnight:05d}"

WORKSPACE_DIR = os.path.abspath(os.path.join("./craneum", workspace_subdir))

print_lock = threading.Lock() 

def safe_print(*args, **kwargs):
    """Thread-safe printing to prevent garbled CLI output."""
    with print_lock:
        print(*args, **kwargs)

def worker_print(step_name: str, message: str, color: str = ""):
    """Helper to print worker logs with indentation and worker ID."""
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
        
    if not os.path.exists(WORKSPACE_DIR):
        os.makedirs(WORKSPACE_DIR)

# --- Execution ---

MODEL_NAME = model_name(args.host, args.port, endpoint="/models")
if MODEL_NAME:
    safe_print(f"✅ Ready: {MODEL_NAME}")

# ==========================================
# SYSTEM PROMPTS
# ==========================================

OVERSIGHT_SYSTEM_PROMPT = """You are the Oversight Orchestrator. Your goal is to fulfill the User Intent by planning a pipeline of tasks.

WORKFLOW:
1. `propose_plan`: Create a visually readable ASCII DAG of the pipeline. Define each step with a specific `sub_prompt`.
2. Wait for user approval or refinement.
3. `execute_workers`: Dispatch the plan. Independent Worker Agents will be spawned for each step. They will ONLY see their specific `sub_prompt` and have no context of the high-level plan.
4. `declare_result`: Once all workers report back, synthesize the final status.

RULES:
- A shared persistent host volume is mounted at `/workspace` for all workers. Instruct them to use this directory to pass files/data to each other.
- Steps with the SAME `parallel_group` ID run CONCURRENTLY.

OUTPUT SCHEMA:
{
  "action_type": "propose_plan" or "execute_workers" or "declare_result",
  "plan_dag": "<ASCII art DAG> or null",
  "pipeline": [
    {
      "step_name": "<unique name>",
      "parallel_group": <integer>,
      "sub_prompt": "<Strict, self-contained instructions for the Worker Agent. e.g., 'Use python to read /workspace/data.json and filter out X, save to /workspace/filtered.json'>"
    }
  ],
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation> or null"
}
"""

WORKER_SYSTEM_PROMPT = """You are an autonomous Worker Agent. You do not know the high-level plan; you only know your specific task.
Your goal is to fulfill your Prompt by running ephemeral Docker containers.

ENVIRONMENT:
1. You can run any `image` (e.g., 'python:3.11-slim', 'node:18', 'ubuntu', 'curlimages/curl').
2. ALWAYS use `/workspace` to read/write files. This is a shared persistent volume.
3. If you need custom code, provide `script_content` and `script_filename`. It will be saved to `/workspace/<script_filename>` before running your command.

WORKFLOW:
1. Loop between `run_container` to test/execute things, and checking results.
2. When your specific prompt is fully satisfied (or permanently failed), use `declare_result`.

OUTPUT SCHEMA:
{
  "action_type": "run_container" or "declare_result",
  "image": "<docker image> or null",
  "script_filename": "<filename.ext> or null",
  "script_content": "<code string> or null",
  "command": "<command to run, e.g., 'python /workspace/script.py'> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation> or null"
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
        return {"action_type": "declare_result", "status": "FAILED", "reason": "Invalid JSON."}

def execute_docker_task(worker_name: str, task: dict) -> dict:
    image = task.get("image", "ubuntu")
    script_filename = task.get("script_filename")
    script_content = task.get("script_content")
    command_str = task.get("command", "")

    if script_filename and script_content:
        script_path = os.path.join(WORKSPACE_DIR, script_filename)
        try:
            with open(script_path, "w") as f:
                f.write(script_content)
            worker_print(worker_name, f"📄 Wrote script to /workspace/{script_filename}")
        except IOError as e:
            return {"exit_code": 1, "stdout": "", "stderr": f"Failed writing script: {e}"}

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{WORKSPACE_DIR}:/workspace",
        "-w", "/workspace",
        image,
        "sh", "-c", command_str
    ]

    worker_print(worker_name, f"🐳 Running {image} -> {command_str}", color="\033[33m") 
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.stdout:
            worker_print(worker_name, f"[STDOUT]\n{res.stdout.strip()}", color="\033[90m") 
        if res.stderr:
            worker_print(worker_name, f"[STDERR]\n{res.stderr.strip()}", color="\033[31m") 
            
        return {
            "exit_code": res.returncode,
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip()
        }
    except Exception as e:
         worker_print(worker_name, f"❌ Execution Exception: {str(e)}", color="\033[31m")
         return {"exit_code": 1, "stdout": "", "stderr": str(e)}

def run_worker_loop(step_name: str, sub_prompt: str, max_turns: int = 13) -> dict:
    messages = [
        {"role": "system", "content": WORKER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Worker Prompt: {sub_prompt}"}
    ]

    worker_print(step_name, "🔄 Worker Loop Started", color="\033[36m") 

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
            status = parsed_action.get('status')
            reason = parsed_action.get('reason')
            icon = "✅" if status == "SUCCESS" else "❌"
            worker_print(step_name, f"{icon} Finished: {status} - {reason}", color="\033[32m" if status == "SUCCESS" else "\033[31m")
            return {"step_name": step_name, "status": status, "reason": reason}
            
        elif action_type == "run_container":
            worker_print(step_name, f"⚙️ Turn {turn + 1}: Executing container task...")
            exec_result = execute_docker_task(step_name, parsed_action)
            
            evidence = (
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            messages.append({"role": "user", "content": evidence})
        else:
            worker_print(step_name, f"⚠️ Unknown action: {action_type}", color="\033[31m")
            return {"step_name": step_name, "status": "FAILED", "reason": f"Unknown action: {action_type}"}

    worker_print(step_name, "⚠️ Max worker turns reached.", color="\033[31m")
    return {"step_name": step_name, "status": "FAILED", "reason": "Max worker turns reached."}

def execute_plan_with_workers(pipeline: List[dict]) -> dict:
    groups = {}
    for task in pipeline:
        g_id = task.get("parallel_group", 1)
        groups.setdefault(g_id, []).append(task)
        
    sorted_groups = sorted(groups.keys())
    pipeline_results = []
    
    for g_id in sorted_groups:
        tasks = groups[g_id]
        safe_print(f"\n🚀 \033[1mStarting Parallel Group {g_id} ({len(tasks)} workers)...\033[0m")
        safe_print("    │") 
        
        group_results = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_task = {
                executor.submit(run_worker_loop, t['step_name'], t['sub_prompt']): t 
                for t in tasks
            }
            for future in as_completed(future_to_task):
                result = future.result()
                group_results.append(result)
                
        pipeline_results.extend(group_results)
        safe_print("    │") 
        
        if any(r['status'] != "SUCCESS" for r in group_results):
            safe_print(f"⚠️ \033[31mGroup {g_id} had worker failures. Halting pipeline oversight.\033[0m")
            break
            
    return pipeline_results

def run_oversight_loop(user_intent: str, session: PromptSession, style: Style, max_turns: int = 8):
    messages = [
        {"role": "system", "content": OVERSIGHT_SYSTEM_PROMPT},
        {"role": "user", "content": f"User Intent: {user_intent}"}
    ]

    for turn in range(max_turns):
        safe_print(f"\n\033[35m\033[1m==== Oversight Turn {turn + 1} ====\033[0m")
        
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
            safe_print(f"\n❇  \033[1m[FINAL RESULT]\033[0m {parsed_action.get('status')}")
            safe_print(f"🧠 \033[1m[REASON]\033[0m {parsed_action.get('reason')}")
            return

        elif action_type == "propose_plan":
            plan_dag = parsed_action.get("plan_dag", "No visual plan provided.")
            pipeline = parsed_action.get("pipeline", [])
            
            safe_print(f"\n\033[36m📋 [PROPOSED OVERSIGHT PLAN]\033[0m\n")
            safe_print(plan_dag)
            safe_print("-" * 50)
            for task in pipeline:
                safe_print(f"🔹 Step: \033[1m{task.get('step_name')}\033[0m (Group {task.get('parallel_group')})")
                safe_print(f"   Prompt: {task.get('sub_prompt')}\n")
            
            user_feedback = session.prompt(
                [('class:prompt', ' APPROVE/REFINE '), ('class:ws', ' '), ('class:prompt', ' Press Enter to approve plan, or type text to refine: ')],
                style=style
            )
            
            if user_feedback.strip() == "":
                safe_print("👍 Plan approved. Dispatched to Worker Agents...")
                messages.append({"role": "user", "content": "The plan is approved. Please proceed with `execute_workers` using the pipeline."})
            else:
                safe_print("🛠️ Plan refinement submitted.")
                messages.append({"role": "user", "content": f"Please refine the plan based on this feedback: {user_feedback}"})
            
        elif action_type == "execute_workers":
            pipeline = parsed_action.get("pipeline", [])
            if not pipeline:
                safe_print("❌ [ERROR] Pipeline empty.")
                break
                
            results = execute_plan_with_workers(pipeline)
            
            evidence = "WORKER EXECUTION RESULTS:\n" + json.dumps(results, indent=2)
            messages.append({"role": "user", "content": evidence})
            
        else:
            safe_print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

    safe_print("\n⚠️ [WARNING] Max oversight turns reached.")

if __name__ == "__main__":
    check_docker()
    safe_print(f"🚀 Dual-Loop Orchestrator Initialized.")
    safe_print(f"📁 Workspace: {WORKSPACE_DIR}")
    safe_print("Type 'exit' to quit.")
    
    prompt_session = PromptSession()
    cli_style = Style.from_dict({
        'llm': 'bg:#c4c408 fg:#000000 bold',   
        'prompt': 'bg:#000000 fg:#c4c408',     
        'ws': 'bg:#c4c408 fg:#c4c408'          
    })
    
    try:
        while True:
            user_input = prompt_session.prompt(
                    [('class:llm', ' CRANEUM '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=cli_style
            )
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_oversight_loop(user_input, prompt_session, cli_style)
    except KeyboardInterrupt:
        safe_print("\nExiting...")
