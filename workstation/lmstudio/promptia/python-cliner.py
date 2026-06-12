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
    help='The port number of the local LLM server'
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
host = args.host
tool_dir = args.tool_dir
config_path = args.config

# Configure for local LLM (e.g., Ollama, vLLM, or LM Studio)
client = OpenAI(
    base_url=f"http://{host}:{port}/v1", 
    api_key="localm" 
)

# Global cache to prevent re-embedding filenames every turn
TOOL_EMBEDDING_CACHE = {}

def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        
        if 'models' in data and data['models']:
            first_model = data['models'][0]
            if 'name' in first_model:
                m_name = first_model['name']
                print(f"✅ Ready: {m_name}")
                return m_name
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
    except Exception as e:
        print(f"\n❌ An unexpected error occurred: {e}")
        return None

def load_threshold(model_id: str, config_file: str, default: float = 0.65) -> float:
    """Loads the model-specific threshold from a YAML file."""
    if not os.path.exists(config_file):
        print(f"ℹ️  [INFO] Config file '{config_file}' not found. Using default threshold: {default}")
        return default

    if not YAML_AVAILABLE:
        print("⚠️ [WARNING] 'pyyaml' is not installed. Cannot parse YAML config. Using default threshold.")
        print("   Run 'pip install pyyaml' to enable dynamic thresholds.")
        return default

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            
        if config and model_id in config:
            raw_threshold = config[model_id].get('similarity_threshold')
            if raw_threshold is not None:
                # Convert whole numbers (e.g., 80) to percentages (0.80)
                if raw_threshold > 1:
                    actual_threshold = float(raw_threshold) / 100.0
                else:
                    actual_threshold = float(raw_threshold)
                
                print(f"⚙️  [CONFIG] similarity_threshold = {actual_threshold}")
                return actual_threshold
            
        print(f"ℹ️  [INFO] No specific threshold found for '{model_id}' in config. Using default: {default}")
        return default
        
    except Exception as e:
        print(f"⚠️ [WARNING] Error reading config file '{config_file}': {e}. Using default threshold.")
        return default

# --- Execution & Logic ---

MODEL_NAME = model_name(host, port, endpoint="/models")
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
    """
    Selects a tool based on semantic similarity between the user intent
    and the formatted filenames in the tool directory.
    """
    if not tool_dir or not os.path.isdir(tool_dir):
        return None

    # 1. Embed the user's intent
    intent_vector = get_embedding(user_intent)
    if not intent_vector:
        return None

    best_match_path = None
    highest_similarity = 0.0

    # 2. Iterate and embed/cache filenames
    for filename in os.listdir(tool_dir):
        if filename.endswith(".py"):
            filepath = os.path.join(tool_dir, filename)
            
            # Clean filename for better semantic meaning (e.g., check_network_status.py -> check network status)
            clean_name = filename[:-3].replace("_", " ").replace("-", " ")

            # Check cache to avoid re-embedding files unnecessarily
            if filepath not in TOOL_EMBEDDING_CACHE:
                file_vector = get_embedding(clean_name)
                if file_vector:
                    TOOL_EMBEDDING_CACHE[filepath] = file_vector
                else:
                    continue

            # 3. Calculate similarity
            similarity = cosine_similarity(intent_vector, TOOL_EMBEDDING_CACHE[filepath])
            
            # Uncomment for debugging threshold values:
            # print(f"   [DEBUG] {clean_name} -> Score: {similarity:.3f}")
            
            if similarity > highest_similarity:
                highest_similarity = similarity
                best_match_path = filepath

    # 4. Return the best match if it clears our confidence threshold
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

def run_agent_loop(user_intent: str, tool_dir: str, threshold: float, max_turns: int = 7):
    """Manages the multi-turn execution and validation loop."""
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

    # --- MAIN LOOP ---
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

    print("\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.")

if __name__ == "__main__":
    if tool_dir == './tools' and not os.path.exists(tool_dir):
        os.makedirs(tool_dir, exist_ok=True)
        
    print(f"🚀 Local LLM Python Agent Initialized. Tool dir: '{tool_dir}'. Type 'exit' to quit.")
    while True:
        try:
            promptia_session = PromptSession()
            promptia_style = Style.from_dict({
                'llm': 'bg:#c4c408 fg:#000000 bold',   
                'prompt': 'bg:#000000 fg:#c4c408',     
                'ws': 'bg:#c4c408 fg:#c4c408'          
            })
            user_input = promptia_session.prompt(
                    [('class:llm', ' PYTHON-CLINER '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
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
