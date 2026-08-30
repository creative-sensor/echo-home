#!/usr/bin/env python

# ---- GLOBAL ----
import os
import json
import subprocess
import re
import argparse
from openai import OpenAI
import requests
from typing import Optional
import signal
from typing import Dict
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
# ---- GLOBAL.end ----

# ---- DEFINITIONS (Classes & Functions) ----
def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f'http://{host}:{port}{endpoint}'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if 'models' in data and data['models']:
            first_model = data['models'][0]
            if 'name' in first_model:
                model_name = first_model['name']
                print(f'✅ Ready: {model_name}')
                return model_name
            else:
                print("⚠️ Warning: Model structure found, but 'name' key is missing.")
                return None
        else:
            print("⚠️ Warning: API response was empty or missing the 'models' list.")
            return None
    except requests.exceptions.ConnectionError:
        print('\n❌ ERROR: Connection Error.')
        print('   Ensure that the API service is running and accessible at the specified host and port.')
        return None
    except requests.exceptions.HTTPError as e:
        print(f'\n❌ ERROR: HTTP Error occurred: {e}')
        print("   Check if the endpoint '/models' is correct and the server supports it.")
        return None
    except requests.exceptions.Timeout:
        print('\n❌ ERROR: The request timed out.')
        return None
    except json.JSONDecodeError:
        print('\n❌ ERROR: Failed to decode JSON. The API might be returning non-JSON data.')
        return None
    except Exception as e:
        print(f'\n❌ An unexpected error occurred: {e}')
        return None

def execute_shell_command(command: str) -> Dict:
    """
    Executes a shell command. Uses subprocess.Popen to allow manual 
    management of the process and guarantee forceful termination 
    (SIGKILL) if the timeout is reached.
    """
    COLOR_BG = '\x1b[48;5;18m'
    COLOR_FG = '\x1b[38;5;231m'
    STOP = '\x1b[0m'
    print(f'\n[SYSTEM] Executing: {COLOR_BG}{COLOR_FG} {command} {STOP}')
    cmd_args = ['bash', '-c', command]
    proc = None
    execbin = None
    if os.environ.get('OS') == 'WINDOW_NT':
        execbin = 'C:\\Program Files\\Git\\bin\\bash.exe'
    try:
        proc = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, executable=execbin)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=30)
            return {'exit_code': proc.returncode, 'stdout': stdout_data.strip(), 'stderr': stderr_data.strip()}
        except subprocess.TimeoutExpired:
            print(f'[SYSTEM] Timeout reached (30s). Forcing termination (SIGKILL).')
            proc.kill()
            proc.wait()
            return {'exit_code': 124, 'stdout': '', 'stderr': 'Command timed out and was forcefully terminated (SIGKILL).'}
    except FileNotFoundError:
        return {'exit_code': 127, 'stdout': '', 'stderr': 'Bash interpreter not found.'}
    except Exception as e:
        return {'exit_code': 1, 'stdout': '', 'stderr': f'An unexpected error occurred: {str(e)}'}
    finally:
        if proc and proc.poll() is None:
            proc.kill()

def parse_llm_json(raw_text: str) -> dict:
    """Cleans up potential markdown from the LLM and parses the JSON."""
    clean_text = re.sub('```json', '', raw_text)
    clean_text = re.sub('```', '', clean_text).strip()
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        print(f'[ERROR] Failed to parse JSON from LLM: {raw_text}')
        return {'action_type': 'declare_result', 'status': 'FAILED', 'reason': 'LLM output invalid JSON.'}

def format_markdown_tables(text: str) -> str:
    """Finds markdown tables in the text and aligns their vertical bars using spaces."""
    if not text:
        return text
    lines = text.split('\n')
    result = []
    table_block = []

    def align_table(tb):
        if len(tb) < 2:
            return tb
        parsed = []
        for r in tb:
            clean_r = r.strip()
            if clean_r.startswith('|'):
                clean_r = clean_r[1:]
            if clean_r.endswith('|'):
                clean_r = clean_r[:-1]
            columns = [c.strip() for c in clean_r.split('|')]
            parsed.append(columns)
        max_cols = max((len(row) for row in parsed))
        widths = [0] * max_cols
        for row_idx, row in enumerate(parsed):
            for i in range(len(row)):
                if row_idx == 1 and '-' in row[i] and (not row[i].replace(':', '').replace('-', '').strip()):
                    continue
                widths[i] = max(widths[i], len(row[i]))
        widths = [max(3, w) for w in widths]
        formatted = []
        for row_idx, row in enumerate(parsed):
            while len(row) < max_cols:
                row.append('')
            f_cols = []
            for i, col in enumerate(row):
                is_separator = row_idx == 1 and '-' in col and (not col.replace(':', '').replace('-', '').strip())
                if is_separator:
                    if col.startswith(':') and col.endswith(':'):
                        f_cols.append(':' + '-' * (widths[i] - 2) + ':')
                    elif col.startswith(':'):
                        f_cols.append(':' + '-' * (widths[i] - 1))
                    elif col.endswith(':'):
                        f_cols.append('-' * (widths[i] - 1) + ':')
                    else:
                        f_cols.append('-' * widths[i])
                else:
                    f_cols.append(col.ljust(widths[i]))
            formatted.append('| ' + ' | '.join(f_cols) + ' |')
        return formatted
    for line in lines:
        stripped = line.strip()
        if '|' in stripped and stripped.startswith('|') and stripped.endswith('|'):
            table_block.append(line)
        else:
            if table_block:
                result.extend(align_table(table_block))
                table_block = []
            result.append(line)
    if table_block:
        result.extend(align_table(table_block))
    return '\n'.join(result)

def run_agent_loop(user_intent: str, max_turns: int=7):
    """Manages the multi-turn execution and validation loop."""
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': f'User Intent: {user_intent}'}]
    for turn in range(max_turns):
        print(f'\n\x1b[36m\x1b[1m---- Turn {turn + 1} ----\x1b[0m')
        response = client.chat.completions.create(model=MODEL_NAME, messages=messages, temperature=0.0, response_format={'type': 'json_object'})
        llm_output = response.choices[0].message.content
        parsed_action = parse_llm_json(llm_output)
        messages.append({'role': 'assistant', 'content': json.dumps(parsed_action)})
        action_type = parsed_action.get('action_type')
        if action_type == 'declare_result':
            status = parsed_action.get('status')
            reason = parsed_action.get('reason')
            if reason:
                reason = format_markdown_tables(reason)
            print(f'\n❇  [FINAL RESULT] {status}')
            print(f'🧠 [REASON] {reason}')
            return
        elif action_type == 'run_command':
            command = parsed_action.get('command')
            if not command:
                print('\n❌ [ERROR] LLM requested run_command but provided no command.')
                break
            exec_result = execute_shell_command(command)
            if exec_result['stdout']:
                print(f"[STDOUT]\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"[STDERR]\n{exec_result['stderr']}")
            if not exec_result['stdout'] and (not exec_result['stderr']):
                print('[OUTPUT] (No standard output or error)')
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            evidence = f"Command Executed: {command}\nExit Code: {exec_result['exit_code']}\nstdout: {exec_result['stdout']}\nstderr: {exec_result['stderr']}"
            messages.append({'role': 'user', 'content': evidence})
        else:
            print(f'\n❌ [ERROR] Unknown action type: {action_type}')
            break
    print('\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.')

# ---- DEFINITIONS.end ----

# ---- MAIN ---
parser = argparse.ArgumentParser(description='Connect to a local OpenAI API endpoint.')
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
parser.add_argument('--prompt', type=str, help='Execute the agent once with the provided prompt and terminate.')
args = parser.parse_args()
port = args.port
host = args.host
client = OpenAI(base_url=f'http://{host}:{port}/v1', api_key='localm')
MODEL_NAME = model_name(host, port, endpoint='/models')
SYSTEM_PROMPT = 'You are an autonomous Kubernetes (k8s) cluster operator. You have access to a terminal where `kubectl` is installed and authenticated to a cluster.\nYour goal is to fulfill the User Intent, verify it actually worked by querying the cluster state, and report the final status.\n\nRULES:\n1. You operate in a loop. You can either issue a shell command (primarily `kubectl`) to execute/verify something, OR you can declare the task finished.\n2. Do not declare SUCCESS unless you have explicit proof from stdout/stderr or by running a verification command (e.g., `kubectl get pods`, `kubectl describe`).\n3. Output ONLY valid JSON for your actions. No markdown blocks around the JSON payload itself. No explanations outside the JSON.\n4. IMPORTANT: When you use `declare_result`, the `reason` field MUST be formatted using Markdown, YAML, or JSON to ensure it is highly human-readable. If showing k8s manifests, use YAML blocks. If listing items, use Markdown tables or bullet points. When creating Markdown tables, ALWAYS align the vertical bars (`|`) perfectly across all upper and lower rows by padding the content with spaces.\n\nOUTPUT SCHEMA:\n{\n  "action_type": "run_command" or "declare_result",\n  "command": "<kubectl or shell command> or null",\n  "status": "SUCCESS" or "FAILED" or null,\n  "reason": "<detailed explanation and heavily formatted output (Markdown/YAML/JSON)> or null"\n}\n'
if __name__ == '__main__':
    if args.prompt:
        print('🚀 Executing agent once with provided prompt...')
        run_agent_loop(args.prompt)
        print('Agent execution complete. Terminating.')
    else:
        print("🚀 Local LLM CLI Agent Initialized. Type 'exit' to quit.")
        while True:
            try:
                COLOR_BG = '\x1b[48;5;11m'
                COLOR_BG2 = '\x1b[48;5;234m'
                COLOR_FG = '\x1b[1;34m\x1b[38;5;234m'
                COLOR_FG2 = '\x1b[38;5;11m'
                STOP = '\x1b[0m'
                promptia_session = PromptSession()
                promptia_style = Style.from_dict({'llm': 'bg:#c4c408 fg:#000000 bold', 'prompt': 'bg:#000000 fg:#c4c408', 'ws': 'bg:#c4c408 fg:#c4c408'})
                user_input = promptia_session.prompt([('class:llm', ' KUBE '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')], multiline=True, style=promptia_style)
                if user_input.strip().lower() in ['exit', 'quit']:
                    break
                if user_input.strip():
                    run_agent_loop(user_input)
            except KeyboardInterrupt:
                print('\nExiting...')
                break
# ---- MAIN.end ----
