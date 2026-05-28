#!/usr/bin/env python
import os
import json
import re
import argparse
import sys
import io
import traceback
from contextlib import redirect_stdout, redirect_stderr
from openai import OpenAI
import requests
from typing import Optional, Dict

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

# --- Execution ---

MODEL_NAME = model_name(
    host, 
    port, 
    endpoint="/models"
)

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

def execute_python_code(code: str, global_state: dict) -> Dict:
    """
    Executes Python code natively using exec() and captures stdout/stderr.
    The global_state dictionary allows variables to persist across multiple execution turns.
    """
    print(f"\n[SYSTEM] Executing Python:\n\033[33m{code}\033[0m")
    
    stdout_trap = io.StringIO()
    stderr_trap = io.StringIO()
    exit_code = 0
    
    try:
        # Redirect standard output and error to capture print statements and native errors
        with redirect_stdout(stdout_trap), redirect_stderr(stderr_trap):
            exec(code, global_state)
    except Exception:
        # Catch any exceptions raised during exec() and dump the traceback to stderr
        traceback.print_exc(file=stderr_trap)
        exit_code = 1
        
    return {
        "exit_code": exit_code,
        "stdout": stdout_trap.getvalue().strip(),
        "stderr": stderr_trap.getvalue().strip()
    }

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
    
    # State dictionary to persist variables between exec() calls in the loop
    agent_globals = {}

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
                
            # Execute the Python code natively
            exec_result = execute_python_code(code, agent_globals)
            
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and not exec_result['stderr']:
                print("[OUTPUT] (No standard output or error)")
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            
            # Format the system evidence for the LLM
            evidence = (
                f"Code Executed:\n{code}\n"
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
    print("🚀 Local LLM Python Agent Initialized. Type 'exit' to quit.")
    while True:
        try:
            promptia_session = PromptSession()
            promptia_style = Style.from_dict({
                'llm': 'bg:#c4c408 fg:#000000 bold',   # yellow background, black text
                'prompt': 'bg:#000000 fg:#c4c408',     # black background, yellow text
                'ws': 'bg:#c4c408 fg:#c4c408'          # white space
            })
            user_input = promptia_session.prompt(
                    [('class:llm', ' PYTHON-CLINER '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
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
