#!/usr/bin/env python
import os
import json
import re
import argparse
import sys
import io
import math
import traceback
from contextlib import redirect_stdout, redirect_stderr
from openai import OpenAI
import requests
from typing import Optional, Dict
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

parser = argparse.ArgumentParser(description="Connect to a local OpenAI API endpoint.")
parser.add_argument(
    '--port', 
    type=int, 
    default=8080, 
    help='The port number of the local LLM server (Apprentice)'
)
parser.add_argument(
    '--port-right', 
    type=int, 
    default=8081, 
    help='The port number of the local Mentor LLM server'
)
parser.add_argument(
    '--host', 
    type=str, 
    default='localhost', 
    help='The hostname of the local LLM server'
)
parser.add_argument(
    '--tool-dir', 
    type=str, 
    default=os.path.expanduser('~/echo-home/codev/python/cliner'), 
    help='Directory containing python scripts for initial tool selection'
)
parser.add_argument(
    '--config', 
    type=str, 
    default='python-cliner/model-settings.yaml', 
    help='Path to the YAML config file for model-specific similarity thresholds'
)
args = parser.parse_args()
port = args.port
port_right = args.port_right
host = args.host
tool_dir = args.tool_dir
config_path = args.config

# Configure Apprentice Client
client = OpenAI(
    base_url=f"http://{host}:{port}/v1", 
    api_key="localm" 
)

# Configure Mentor Client
client_mentor = OpenAI(
    base_url=f"http://{host}:{port_right}/v1", 
    api_key="localm" 
)

# Global cache to prevent re-embedding filenames every turn
TOOL_EMBEDDING_CACHE = {}

def model_name(host: str, port: int, endpoint: str, role: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        
        if 'models' in data and data['models']:
            first_model = data['models'][0]
            if 'name' in first_model:
                m_name = first_model['name']
                print(f"✅ Ready [{role}]: {m_name} (Port: {port})")
                return m_name
            else:
                print(f"⚠️ Warning [{role}]: Model structure found, but 'name' key is missing.")
                return None
        else:
            print(f"⚠️ Warning [{role}]: API response was empty or missing the 'models' list.")
            return None

    except requests.exceptions.ConnectionError:
        if role == "Mentor":
             print(f"\nℹ️  [INFO] Mentor API not found on port {port}. Running in Apprentice-only mode.")
        else:
             print("\n❌ ERROR: Connection Error.")
             print(f"   Ensure that the {role} API service is running and accessible at {host}:{port}.")
        return None
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
        return None

def load_threshold(model_id: str, config_file: str, default: float = 0.65) -> float:
    """Loads the model-specific threshold from a YAML file."""
    if not os.path.exists(config_file):
        return default

    if not YAML_AVAILABLE:
        return default

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            
        if config and model_id in config:
            raw_threshold = config[model_id].get('similarity_threshold')
            if raw_threshold is not None:
                if raw_threshold > 1:
                    actual_threshold = float(raw_threshold) / 100.0
                else:
                    actual_threshold = float(raw_threshold)
                return actual_threshold
        return default
        
    except Exception:
        return default

# --- Execution & Logic ---

MODEL_NAME = model_name(host, port, endpoint="/models", role="Apprentice")
MENTOR_MODEL_NAME = model_name(host, port_right, endpoint="/models", role="Mentor")
ACTIVE_THRESHOLD = load_threshold(MODEL_NAME, config_path, default=0.65) if MODEL_NAME else 0.65

SYSTEM_PROMPT = """You are an autonomous Python agent. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

RULES:
1. You operate in a loop. You can either write inline, short, and compact Python code to execute/verify something natively, OR you can declare the task finished.
2. The Python code runs in the same persistent interpreter session across turns. Variables you define in one turn are available in the next.
3. Do not declare SUCCESS unless you have explicit proof from stdout/stderr. Use `print()` in your code to output results so you can see them.
4. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_python" or "declare_result",
  "code": "<python code string> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}
"""

def get_embedding(text: str) -> list[float]:
    """Fetches text vector embeddings from the local API."""
    try:
        response = client.embeddings.create(
            input=text,
            model=MODEL_NAME
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"⚠️ [WARNING] Failed to generate embedding: {e}")
        return []

def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculates cosine similarity natively without numpy."""
    if not vec1 or not vec2:
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = math.sqrt(sum(a * a for a in vec1))
    mag2 = math.sqrt(sum(b * b for b in vec2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot_product / (mag1 * mag2)

def find_relevant_tool_embedded(user_intent: str, tool_dir: str, threshold: float = 0.5) -> Optional[str]:
    """Selects a tool based on semantic similarity between the user intent and filenames."""
    if not tool_dir or not os.path.isdir(tool_dir):
        return None

    intent_vector = get_embedding(user_intent)
    if not intent_vector:
        return None

    best_match_path = None
    highest_similarity = 0.0

    for filename in os.listdir(tool_dir):
        if filename.endswith(".py"):
            filepath = os.path.join(tool_dir, filename)
            clean_name = filename[:-3].replace("_", " ").replace("-", " ")

            if filepath not in TOOL_EMBEDDING_CACHE:
                file_vector = get_embedding(clean_name)
                if file_vector:
                    TOOL_EMBEDDING_CACHE[filepath] = file_vector
                else:
                    continue

            similarity = cosine_similarity(intent_vector, TOOL_EMBEDDING_CACHE[filepath])
            
            if similarity > highest_similarity:
                highest_similarity = similarity
                best_match_path = filepath

    if highest_similarity >= threshold:
        return best_match_path
    return None

def execute_python_code(code: str, global_state: dict) -> Dict:
    """Executes Python code natively using exec() and captures stdout/stderr."""
    print(f"\n[SYSTEM] Executing Python:\n\033[33m{code}\033[0m")
    
    stdout_trap = io.StringIO()
    stderr_trap = io.StringIO()
    exit_code = 0
    
    try:
        with redirect_stdout(stdout_trap), redirect_stderr(stderr_trap):
            exec(code, global_state)
    except Exception:
        traceback.print_exc(file=stderr_trap)
        exit_code = 1
        
    return {
        "exit_code": exit_code,
        "stdout": stdout_trap.getvalue().strip(),
        "stderr": stderr_trap.getvalue().strip()
    }

def parse_llm_json(raw_text: str) -> dict:
    """Cleans up potential markdown from the LLM and parses the JSON."""
    clean_text = re.sub(r"```json", "", raw_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"```", "", clean_text).strip()
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        print(f"[ERROR] Failed to parse JSON from LLM: {raw_text}")
        return {"action_type": "declare_result", "status": "FAILED", "reason": "LLM output invalid JSON."}




def call_mentor_for_extension(apprentice_messages: list) -> str:
    """Calls the Mentor model when the Apprentice is simply out of turns."""
    mentor_system_prompt = """You are an expert developer overseeing an autonomous Python agent. 
The agent has run out of execution turns and is spinning its wheels.
Review the conversation history and write a response AS THE USER to guide the agent toward the correct solution.

RULES FOR YOUR RESPONSE:
1. Speak directly to the agent like a human user giving a straightforward hint (e.g., "You are stuck because of X. Try doing Y using the `subprocess` module.").
2. DO NOT hallucinate tools. The agent ONLY knows how to execute native Python code. If you need it to run a system command, explicitly tell it to write a Python script using `subprocess.run()`.
3. Keep your guidance concise, natural, and strictly focused on the Python-based solution. Do NOT mention JSON, formatting, or output schemas."""

    mentor_messages = [{"role": "system", "content": mentor_system_prompt}]
    mentor_messages.extend(apprentice_messages[1:])

    try:
        response = client_mentor.chat.completions.create(
            model=MENTOR_MODEL_NAME,
            messages=mentor_messages,
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"An error occurred on my end: {e}. Please review the variables and try a simpler Python approach."

def call_mentor_for_failure(apprentice_messages: list) -> str:
    """Calls the Mentor model when the Apprentice explicitly declares a FAILED status."""
    mentor_system_prompt = """You are an expert developer overseeing an autonomous Python agent. 
The agent just failed a task and declared a 'FAILED' status. 
Review the conversation history, identify why the agent's Python code failed, and write a response AS THE USER to guide it back on track.

RULES FOR YOUR RESPONSE:
1. Speak directly to the agent like a human user giving a straightforward hint (e.g., "That failed because you assumed this was a Windows environment. Try doing X instead using the `platform` module.").
2. DO NOT hallucinate tools. The agent ONLY knows how to execute native Python code. If you need it to run a system command, explicitly tell it to write a Python script using `subprocess.run()`.
3. Keep your guidance concise, natural, and strictly focused on the Python-based solution. Do NOT mention JSON, formatting, or output schemas."""

    mentor_messages = [{"role": "system", "content": mentor_system_prompt}]
    mentor_messages.extend(apprentice_messages[1:])

    try:
        response = client_mentor.chat.completions.create(
            model=MENTOR_MODEL_NAME,
            messages=mentor_messages,
            temperature=0.0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"An error occurred on my end: {e}. Please rethink the problem and attempt a completely different Python library."


def run_agent_loop(user_intent: str, tool_dir: str, threshold: float, max_turns: int = 7):
    """Manages the multi-turn execution and validation loop with Dual Loop Mentor support."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    
    agent_globals = {}
    
    # --- FIRST ATTEMPT: Semantic Tool Search ---
    print(f"\n\033[34m[SYSTEM] Performing semantic vector search for tools (Threshold: {threshold})...\033[0m")
    tool_path = find_relevant_tool_embedded(user_intent, tool_dir, threshold)
    
    if tool_path:
        tool_filename = os.path.basename(tool_path)
        print(f"\033[32m[SYSTEM] Semantic match found: {tool_filename}. Attempting execution...\033[0m")
        try:
            with open(tool_path, 'r', encoding='utf-8') as f:
                tool_code = f.read()
            
            exec_result = execute_python_code(tool_code, agent_globals)
            
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and not exec_result['stderr']:
                print("[OUTPUT] (No standard output or error)")
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            
            evidence = (
                f"Tool '{tool_filename}' Executed:\n{tool_code}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            
            initial_message = (
                f"User Intent: {user_intent}\n\n"
                f"A local script was automatically selected and executed to attempt to fulfill this intent:\n{evidence}\n\n"
                f"Evaluate this output. If the execution successfully resolved the User Intent, declare SUCCESS. "
                f"If it FAILED or did not meet the requirement, generate new 'run_python' code to fix/refine it."
            )
            messages.append({"role": "user", "content": initial_message})
            
        except Exception as e:
            print(f"❌ [ERROR] Failed to read or execute tool '{tool_filename}': {e}")
            messages.append({"role": "user", "content": f"User Intent: {user_intent}"})
    else:
        print("\033[33m[SYSTEM] No relevant tool met the similarity threshold. Falling back to LLM.\033[0m")
        messages.append({"role": "user", "content": f"User Intent: {user_intent}"})

    # --- MAIN LOOP (APPRENTICE + MENTOR) ---
    turn = 0
    budget_extensions = 0
    max_mentor_interventions = 2  # Hard cap to prevent infinite dual-loops

    while turn < max_turns:
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
            
            # --- MENTOR FAILURE INTERCEPTION ---
            if str(status).upper() != "SUCCESS" and MENTOR_MODEL_NAME and budget_extensions < max_mentor_interventions:
                print("\n\033[48;5;8m\033[97m [MENTOR INTERVENTION] Apprentice declared failure. Forcing pivot... \033[0m")
                
                mentor_guidance = call_mentor_for_failure(messages)
                print(f"\n\033[31m{mentor_guidance}\033[0m")
                
                messages.append({"role": "user", "content": mentor_guidance})
                
                max_turns += 5
                budget_extensions += 1
                print(f"\n\033[32m[SYSTEM] Failure intercepted. Budget extended (+5 turns). Resuming Apprentice Loop...\033[0m")
                
                turn += 1
                continue # Skip the rest of the loop and immediately prompt the Apprentice again
            
            # Exit loop if it was a SUCCESS or we have no Mentor/extensions left
            return
            
        elif action_type == "run_python":
            code = parsed_action.get("code")
            if not code:
                print("\n❌ [ERROR] LLM requested run_python but provided no code.")
                break
                
            exec_result = execute_python_code(code, agent_globals)
            
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and not exec_result['stderr']:
                print("[OUTPUT] (No standard output or error)")
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            
            evidence = (
                f"Code Executed:\n{code}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {exec_result['stdout']}\n"
                f"stderr: {exec_result['stderr']}"
            )
            
            messages.append({"role": "user", "content": evidence})
            
        else:
            print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

        turn += 1

        # --- MENTOR OUT-OF-TURNS TRIGGER ---
        if turn == max_turns:
            if MENTOR_MODEL_NAME and budget_extensions < max_mentor_interventions:
                print("\n\033[45m\033[97m [MENTOR INTERVENTION] Apprentice ran out of turns. Synthesizing new directives... \033[0m")
                
                mentor_guidance = call_mentor_for_extension(messages)
                print(f"\n\033[35m{mentor_guidance}\033[0m")
                
                messages.append({"role": "user", "content": mentor_guidance})
                
                max_turns += 5
                budget_extensions += 1
                print(f"\n\033[32m[SYSTEM] Budget extended (+5 turns). Resuming Apprentice Loop...\033[0m")
            elif turn == max_turns:
                 print("\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.")

if __name__ == "__main__":
    if tool_dir == './tools' and not os.path.exists(tool_dir):
        os.makedirs(tool_dir, exist_ok=True)
        
    print(f"🚀 Local Dual-Loop Agent Initialized. Tool dir: '{tool_dir}'. Type 'exit' to quit.")
    while True:
        try:
            promptia_session = PromptSession()
            promptia_style = Style.from_dict({
                'llm': 'bg:#c4c408 fg:#000000 bold',   
                'prompt': 'bg:#000000 fg:#c4c408',     
                'ws': 'bg:#c4c408 fg:#c4c408'          
            })
            user_input = promptia_session.prompt(
                    [('class:llm', ' HEMISPHERE '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=promptia_style
            )
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_agent_loop(user_input, tool_dir, ACTIVE_THRESHOLD)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
