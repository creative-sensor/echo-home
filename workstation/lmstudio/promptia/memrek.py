#!/usr/bin/env python
import os
import json
import subprocess
import argparse
import platform
import re
from typing import Optional, Dict, List
import requests
from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.completion import Completer, Completion, PathCompleter

# --- Setup and Arguments ---
parser = argparse.ArgumentParser(description="Connect to a local OpenAI API endpoint to record workflows.")
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
parser.add_argument('--path', type=str, default=None, help='Path to an existing workflow markdown document to load')
args = parser.parse_args()

client = OpenAI(
    base_url=f"http://{args.host}:{args.port}/v1", 
    api_key="localm" 
)

# --- Shell Auto-Completer ---
class ShellCompleter(Completer):
    """Provides bash-like tab auto-completion for system commands and file paths."""
    def __init__(self):
        self.path_completer = PathCompleter(expanduser=True)
        self.commands = set()
        
        path_env = os.environ.get("PATH", "")
        for path_dir in path_env.split(os.pathsep):
            if os.path.isdir(path_dir):
                try:
                    for exe in os.listdir(path_dir):
                        self.commands.add(exe)
                except PermissionError:
                    continue
                    
    def get_completions(self, document, complete_event):
        text_before_cursor = document.text_before_cursor
        
        if ' ' not in text_before_cursor:
            word = document.get_word_before_cursor()
            for cmd in self.commands:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
        else:
            yield from self.path_completer.get_completions(document, complete_event)

# --- Helper Functions ---
def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if 'models' in data and data['models']:
            return data['models'][0].get('name')
    except Exception as e:
        print(f"❌ Error fetching model: {e}")
        return None

def get_os_context() -> str:
    os_system = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    return f"OS: {os_system} {os_release} (Architecture: {arch})"

# --- LLM Configuration ---
MODEL_NAME = model_name(args.host, args.port, endpoint="/models")

SYSTEM_PROMPT = """You are a Workflow DAG Recorder agent. 
Your goal is to review a user's command history and their stated intent, then consolidate this into a single loosely-coupled Makefile DAG target.

CURRENT ENVIRONMENT:
{os_context}

RULES:
1. Output ONLY the raw text for the Makefile block. Do not wrap it in markdown ```makefile code blocks.
2. Format the target EXACTLY using the following fields in this exact order (include the # for comments):

<TARGET_NAME>: <DEPENDENCIES>
\t# Description: <User hint intent or>
\t# Command: `<The actual command(s) executed>`
\t# Success: <Criteria consider successful target the to whole>
\t# Failure: <Criteria a consider failure target the to whole>
\t# Input: <What from information other retrieve targets to>
\t# Output: <What extract for from information next stdout target the to>

3. VERY IMPORTANT: Do NOT put executable commands at the end of the target. This workflow uses Makefile syntax loosely for documentation only; it is not meant to be executed by the `make` tool. All command references must stay inside the '# Command: `<command>`' comment field.
4. Auto-generate a concise, descriptive <TARGET_NAME> (e.g., `check_disk_space`, `move_card`) in snake_case based on the user's intent and command history.
5. If the user does not explicitly define Input/Output/Success/Failure, infer logical criteria based on the standard output and standard error from their command history.
"""

def execute_shell_command(command: str) -> Dict:
    cmd_args = ["bash", "-c", command]
    execbin = r"C:\Program Files\Git\bin\bash.exe" if os.environ.get('OS') == 'Windows_NT' else None
    
    try:
        proc = subprocess.Popen(
            cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, executable=execbin
        )
        stdout_data, stderr_data = proc.communicate(timeout=30)
        return {"exit_code": proc.returncode, "stdout": stdout_data.strip(), "stderr": stderr_data.strip()}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Error: {str(e)}"}

def generate_dag_node(history: List[Dict], user_context: Dict) -> str:
    formatted_system_prompt = SYSTEM_PROMPT.format(os_context=get_os_context())
    
    history_text = ""
    for idx, item in enumerate(history, 1):
        history_text += f"\n--- Command {idx} ---\nCmd: {item['cmd']}\nExit: {item['result']['exit_code']}\nStdout: {item['result']['stdout']}\nStderr: {item['result']['stderr']}\n"
    
    prompt = f"""
    DEPENDENCIES: {user_context['dependencies']}
    USER EXPLANATION / INTENT: {user_context['intent']}
    
    COMMAND HISTORY TO ANALYZE:
    {history_text}
    
    Synthesize the above into the strict DAG target format, and auto-generate an appropriate TARGET_NAME.
    """
    
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": formatted_system_prompt},
            {"role": "user", "content": prompt}
        ],
        temperature=0.1
    )
    return response.choices[0].message.content.replace("```makefile", "").replace("```", "").strip()

# --- Main Execution Loop ---
if __name__ == "__main__":
    print("🚀 DAG Workflow Recorder Initialized.")
    
    document_content = ""
    step_counter = 1
    
    # Load document raw text if path is provided
    if args.path and os.path.exists(args.path):
        print(f"📂 Loading existing document exactly as is from: {args.path}")
        with open(args.path, 'r', encoding='utf-8') as f:
            document_content = f.read()
        
        if not document_content.endswith("\n"):
            document_content += "\n"
            
        # Estimate the next step number by counting existing "### " headers
        existing_steps = len(re.findall(r'^### \d+\.', document_content, flags=re.MULTILINE))
        if existing_steps == 0:
            # Fallback if numbering isn't standard
            existing_steps = document_content.count("### ")
        
        step_counter = existing_steps + 1
        print(f"✅ Loaded. Next step will be recorded as step {step_counter}.")
    else:
        if args.path:
            print(f"⚠️ File '{args.path}' not found. Starting fresh.")
        workflow_name = input("Enter Workflow Name: ").strip() or "Untitled Workflow"
        instructions = input("Enter global Instructions (or leave blank): ").strip()
        
        document_content = f"# {workflow_name}\n## Instruction\n"
        if instructions:
            document_content += f"{instructions}\n"
        document_content += "## Workflow\n"
    
    print("\n📝 Agent Memory is active (stdout only).")
    print("Type shell commands to execute and record them silently. Use TAB for auto-completion.")
    print("Type '====' to clear the current command history buffer (preserves document in memory).")
    print("Type '===p' to preview the currently generated document.")
    print("Type '===t' to consolidate recent commands into a DAG target and print the document.")
    print("Type '!exit' to quit.\n")
    
    command_buffer = []
    
    promptia_session = PromptSession(completer=ShellCompleter())
    promptia_style = Style.from_dict({
        'llm': 'bg:#c4c408 fg:#000000 bold',
        'prompt': 'bg:#000000 fg:#c4c408',  
        'ws': 'bg:#c4c408 fg:#c4c408'       
    })
    
    while True:
        try:
            cwd = os.path.basename(os.getcwd()) or "/"
            prompt_str = f" {cwd} "
            
            user_input = promptia_session.prompt(
                    [('class:llm', ' MEMREK 🔴 '), ('class:prompt', prompt_str), ('class:ws', ' ')],
                    multiline=False,
                    style=promptia_style
            )
            
            cmd = user_input.strip()
            if not cmd:
                continue
                
            if cmd == '!exit':
                print("\nExiting. Final Document:")
                print(document_content)
                break
                
            elif cmd == '====':
                command_buffer.clear()
                print("\n🧹 Command history buffer cleared. Document and previous workflows remain intact.\n")
                continue
                
            elif cmd == '===p':
                print("\n\033[48;5;54m\033[38;5;177m----  Document Preview ----\033[0m")
                print(f"\033[38;5;92m{document_content}\033[0m")
                print("---------------------------\n")
                continue
                
            elif cmd == '===t':
                if not command_buffer:
                    print("⚠️ No commands in the buffer to consolidate. Run some commands first.")
                    continue
                    
                print("\n--- 🧠 Agent Consolidation Phase ---")
                step_name = input(f"Agent: What is the title for step {step_counter}? (e.g., 'do a task')\n> ").strip()
                deps = input("Agent: Any dependency targets? (leave blank if none)\n> ").strip()
                intent = input("Agent: Briefly describe your overall intent, what inputs you needed, and what output you expect:\n> ").strip()
                
                user_context = {
                    "dependencies": deps,
                    "intent": intent
                }
                
                print("\nGenerating consolidated DAG node...")
                node_markdown = generate_dag_node(command_buffer, user_context)
                
                # Append the new block to the raw string
                new_block = f"### {step_counter}. {step_name}\n```makefile\n{node_markdown}\n```\n\n"
                document_content += new_block
                
                step_counter += 1
                command_buffer.clear()
                
                print("\n✅ Target consolidated. Current Document State:\n")
                print("\033[38;5;92m=========================================")
                print(document_content.strip())
                print("=========================================\033[0m\n")
                
            elif cmd.startswith('cd ') or cmd == 'cd':
                target_dir = cmd[3:].strip() if cmd.startswith('cd ') else "~"
                target_dir = os.path.expanduser(target_dir)
                try:
                    os.chdir(target_dir)
                    result = {"exit_code": 0, "stdout": f"Directory changed to {os.getcwd()}", "stderr": ""}
                    command_buffer.append({"cmd": cmd, "result": result})
                except FileNotFoundError:
                    err_msg = f"bash: cd: {target_dir}: No such file or directory"
                    print(f"[STDERR]\n{err_msg}")
                    command_buffer.append({"cmd": cmd, "result": {"exit_code": 1, "stdout": "", "stderr": err_msg}})
                except Exception as e:
                    print(f"[STDERR]\nError changing directory: {e}")
            else:
                result = execute_shell_command(cmd)
                command_buffer.append({"cmd": cmd, "result": result})
                
                if result['stdout']:
                    print(f"[STDOUT]\n{result['stdout']}")
                if result['stderr']:
                    print(f"[STDERR]\n{result['stderr']}")
                    
        except KeyboardInterrupt:
            print("\nExiting. Final Document:")
            print(document_content)
            break
