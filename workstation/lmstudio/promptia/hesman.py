#!/usr/bin/env python
import os
import json
import re
import argparse
import sys
import threading
import uuid
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
import requests
from typing import Optional, Dict, List

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from kubernetes import client, config

parser = argparse.ArgumentParser(description="Connect to a local OpenAI API endpoint.")
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
parser.add_argument('--plan', type=str, default=None, help='Optional plan name to use as the workspace subdirectory')
args = parser.parse_args()

client_openai = OpenAI(base_url=f"http://{args.host}:{args.port}/v1", api_key="localm")

# --- System Configuration ---
if args.plan:
    workspace_subdir = args.plan
else:
    now = datetime.now()
    seconds_since_midnight = (now.hour * 3600) + (now.minute * 60) + now.second
    workspace_subdir = f"{now.strftime('%Y-%m-%d')}-{seconds_since_midnight:05d}"

WORKSPACE_DIR = os.path.abspath(os.path.join("./hesman", workspace_subdir))

print_lock = threading.Lock() 

def safe_print(*args_print, **kwargs):
    """Thread-safe printing to prevent garbled CLI output."""
    with print_lock:
        print(*args_print, **kwargs)

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

def check_k8s():
    try:
        config.load_kube_config()
        v1 = client.CoreV1Api()
        v1.list_namespace(timeout_seconds=5)
    except Exception as e:
        safe_print(f"❌ ERROR: Kubernetes is not accessible. Ensure your kubeconfig is valid and the cluster is running.\nDetails: {e}")
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

WORKER_SYSTEM_PROMPT = """You are an autonomous Kubernetes Worker Agent. You do not know the high-level plan; you only know your specific task.
Your goal is to fulfill your Prompt by managing Kubernetes resources and executing local commands natively.

ENVIRONMENT:
1. You have a shared persistent directory mapped to `/workspace`. Use this to store your YAML manifests, Helm values, or configuration files.
2. You can execute ANY shell command available on the host (e.g., `kubectl`, `helm`, `cat`, `grep`, `curl`).

WORKFLOW:
1. `write_file`: Prepare your YAML files, scripts, or configs and save them to `/workspace`.
2. `run_shell_command`: Execute commands to interact with the cluster or process files (e.g., `kubectl apply -f /workspace/app.yaml`, `helm install ...`, `kubectl get pods`).
3. Loop between actions to verify your resources are running properly.
4. When your specific prompt is fully satisfied (or permanently failed), use `declare_result`.

OUTPUT SCHEMA:
{
  "action_type": "write_file" or "run_shell_command" or "declare_result",
  "filename": "<filename.ext> (only for write_file)",
  "content": "<file content string> (only for write_file)",
  "command": "<shell command to run> (only for run_shell_command)",
  "status": "SUCCESS" or "FAILED" (only for declare_result),
  "reason": "<explanation> (only for declare_result)"
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

def execute_k8s_task(worker_name: str, task: dict) -> dict:
    image = task.get("image", "ubuntu")
    script_filename = task.get("script_filename")
    script_content = task.get("script_content")
    command_str = task.get("command", "")

    # Write the file directly to the host volume mapping
    if script_filename and script_content:
        script_path = os.path.join(WORKSPACE_DIR, script_filename)
        try:
            with open(script_path, "w") as f:
                f.write(script_content)
            worker_print(worker_name, f"📄 Wrote script to host volume /workspace/{script_filename}")
        except IOError as e:
            return {"exit_code": 1, "stdout": "", "stderr": f"Failed writing script locally: {e}"}

    try:
        config.load_kube_config()
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Kubernetes config error: {e}"}

    v1 = client.CoreV1Api()
    pod_name = f"worker-{uuid.uuid4().hex[:8]}"

    # Pod manifest configured with a hostPath matching the local WORKSPACE_DIR
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "worker-container",
                "image": image,
                "workingDir": "/workspace",
                # Hardcoded /bin/sh is safer across distros than just 'sh'
                "command": ["/bin/sh", "-c", command_str],
                "volumeMounts": [{"name": "workspace-vol", "mountPath": "/workspace"}]
            }],
            "volumes": [{
                "name": "workspace-vol",
                "hostPath": {
                    "path": WORKSPACE_DIR,
                    "type": "DirectoryOrCreate"
                }
            }]
        }
    }

    worker_print(worker_name, f"☸️ Spawning Pod {pod_name} ({image}) -> {command_str}", color="\033[33m") 
    
    try:
        v1.create_namespaced_pod(namespace="default", body=pod_manifest)

        # Poll until pod completes execution
        while True:
            pod = v1.read_namespaced_pod(name=pod_name, namespace="default")
            if pod.status.phase in ['Succeeded', 'Failed']:
                break
            time.sleep(2)

        # Buffer to ensure Kubelet has time to flush logs for extremely short-lived pods
        time.sleep(1)

        # Retrieve logs directly from Kubernetes
        # _preload_content=False prevents the K8s client from applying its own string casting
        try:
            log_response = v1.read_namespaced_pod_log(
                name=pod_name, 
                namespace="default", 
                _preload_content=False
            )
            log = log_response.read().decode('utf-8')
        except Exception as log_err:
            log = f"Failed to read logs: {log_err}"
        
        # Determine exact exit code
        exit_code = 1
        if pod.status.phase == 'Succeeded':
            exit_code = 0
        elif pod.status.container_statuses and pod.status.container_statuses[0].state.terminated:
            exit_code = pod.status.container_statuses[0].state.terminated.exit_code

        if log:
            worker_print(worker_name, f"[LOGS]\n{log.strip()}", color="\033[90m") 
            
        # Clean up the pod after execution
        v1.delete_namespaced_pod(name=pod_name, namespace="default")

        # Kubernetes aggregates stdout/stderr. Route it based on exit code for the LLM.
        return {
            "exit_code": exit_code,
            "stdout": log.strip() if exit_code == 0 and log else "",
            "stderr": log.strip() if exit_code != 0 and log else ""
        }
        
    except Exception as e:
         worker_print(worker_name, f"❌ Kubernetes Execution Exception: {str(e)}", color="\033[31m")
         return {"exit_code": 1, "stdout": "", "stderr": str(e)}


def run_worker_loop(step_name: str, sub_prompt: str, max_turns: int = 15) -> dict:
    import subprocess 
    
    messages = [
        {"role": "system", "content": WORKER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Worker Prompt: {sub_prompt}"}
    ]

    worker_print(step_name, "🔄 Worker Loop Started", color="\033[36m") 

    for turn in range(max_turns):
        response = client_openai.chat.completions.create(
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
            
        elif action_type == "write_file":
            filename = parsed_action.get("filename")
            content = parsed_action.get("content")
            
            if not filename or not content:
                msg = "Error: filename or content missing."
                worker_print(step_name, f"⚠️ {msg}", color="\033[31m")
                messages.append({"role": "user", "content": msg})
                continue

            safe_filename = os.path.basename(filename)
            file_path = os.path.join(WORKSPACE_DIR, safe_filename)
            
            try:
                with open(file_path, "w") as f:
                    f.write(content)
                worker_print(step_name, f"📄 Wrote file to /workspace/{safe_filename}")
                messages.append({"role": "user", "content": f"Success: Wrote file to /workspace/{safe_filename}"})
            except Exception as e:
                worker_print(step_name, f"❌ Failed to write file: {e}", color="\033[31m")
                messages.append({"role": "user", "content": f"Error writing file: {e}"})

        elif action_type == "run_shell_command":
            command = parsed_action.get("command", "")
            if not command:
                msg = "Error: command is empty."
                worker_print(step_name, f"⚠️ {msg}", color="\033[31m")
                messages.append({"role": "user", "content": msg})
                continue

            worker_print(step_name, f"🐚 Running command -> {command}", color="\033[33m") 
            
            # Translate the LLM's virtual '/workspace' path to the host's actual directory path
            actual_command = command.replace("/workspace", WORKSPACE_DIR)
            
            try:
                # Setting cwd ensures commands like 'helm install chart ./' execute in the right context
                res = subprocess.run(
                    actual_command, 
                    shell=True, 
                    capture_output=True, 
                    text=True, 
                    cwd=WORKSPACE_DIR
                )
                
                if res.stdout:
                    worker_print(step_name, f"[STDOUT]\n{res.stdout.strip()}", color="\033[90m") 
                if res.stderr:
                    worker_print(step_name, f"[STDERR]\n{res.stderr.strip()}", color="\033[31m") 
                    
                evidence = (
                    f"Exit Code: {res.returncode}\n"
                    f"stdout: {res.stdout.strip()}\n"
                    f"stderr: {res.stderr.strip()}"
                )
                messages.append({"role": "user", "content": evidence})
            except Exception as e:
                worker_print(step_name, f"❌ Execution Exception: {str(e)}", color="\033[31m")
                messages.append({"role": "user", "content": f"Exception executing command: {e}"})

        else:
            worker_print(step_name, f"⚠️ Unknown action: {action_type}", color="\033[31m")
            messages.append({"role": "user", "content": f"Error: Unknown action '{action_type}'"})

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
        
        response = client_openai.chat.completions.create(
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
    check_k8s()
    safe_print(f"🚀 K8s Dual-Loop Orchestrator Initialized.")
    safe_print(f"📁 Host Workspace: {WORKSPACE_DIR}")
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
                    [('class:llm', ' HESMAN '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=cli_style
            )
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_oversight_loop(user_input, prompt_session, cli_style)
    except KeyboardInterrupt:
        safe_print("\nExiting...")
