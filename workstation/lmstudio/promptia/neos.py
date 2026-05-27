#!/usr/bin/env python
import os
import json
import argparse
import re
import pynvim
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
    except Exception as e:
        print(f"\n❌ ERROR fetching model: {e}")
        return None

# --- Execution ---

MODEL_NAME = model_name(host, port, endpoint="/models")

# Highly focused Neovim Operator prompt
SYSTEM_PROMPT = """You are an autonomous Neovim operator. You have direct access to the Neovim API. Your goal is to fulfill the User Intent inside their current editor session.

RULES:
1. You operate in a loop. You can issue a Neovim Ex command or declare the task finished.
2. Focus strictly on Vim actions.
3. CUSTOM CAPABILITIES: The user has custom functions and keymaps. If you need to know what custom workflows are available, run the command `:AgentCapabilities`. This will return a JSON dictionary of custom commands you can use.
4. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_command" or "declare_result",
  "command": "<Neovim Ex Command> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}
"""

def execute_nvim_command(command: str) -> Dict:
    """
    Executes a command directly in the running Neovim instance via RPC.
    Requires the script to be run from a Neovim terminal (reads $NVIM).
    """
    print(f"\n[SYSTEM] Executing Neovim Command: {command}")
    
    nvim_address = os.environ.get('NVIM')
    if not nvim_address:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "The $NVIM environment variable is missing. Please run this script from inside a Neovim terminal (:term)."
        }
        
    try:
        # Attach to the current Neovim instance
        nvim = pynvim.attach('socket', path=nvim_address)
        
        # command_output executes an Ex command and captures the output
        output = nvim.command_output(command)
        
        return {
            "exit_code": 0,
            "stdout": output.strip() if output else "(Command executed successfully with no output)",
            "stderr": ""
        }
    except pynvim.api.nvim.NvimError as e:
        # Catch standard Vim errors (e.g., E486: Pattern not found)
        return {"exit_code": 1, "stdout": "", "stderr": f"Neovim Error: {str(e)}"}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Unexpected Error: {str(e)}"}

def parse_llm_json(raw_text: str) -> dict:
    """Cleans up potential markdown from the LLM and parses the JSON."""
    clean_text = re.sub(r"```json", "", raw_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"```", "", clean_text).strip()
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        print(f"[ERROR] Failed to parse JSON from LLM: {raw_text}")
        return {"action_type": "declare_result", "status": "FAILED", "reason": "LLM output invalid JSON."}

def run_agent_loop(user_intent: str, max_turns: int = 7):
    """Manages the multi-turn Neovim execution and validation loop."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
                
            # Execute the Neovim API command
            exec_result = execute_nvim_command(command)
            
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            
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
    if not os.environ.get('NVIM'):
        print("\n⚠️  WARNING: $NVIM environment variable not detected.")
        print("   This script is designed to manipulate Neovim using its RPC API.")
        print("   Please run this directly from a terminal inside a running Neovim instance (e.g., `:term`).\n")

    print("🚀 Local Neovim LLM Agent Initialized. Type 'exit' to quit.")
    
    while True:
        try:
            promptia_session = PromptSession()
            promptia_style = Style.from_dict({
                'llm': 'bg:#c4c408 fg:#000000 bold',
                'prompt': 'bg:#000000 fg:#c4c408',
                'ws': 'bg:#c4c408 fg:#c4c408'
            })
            user_input = promptia_session.prompt(
                    [('class:llm', ' NEOS '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
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
