#!/usr/bin/env python

# ---- GLOBAL ----
import argparse
import os
import subprocess
import requests
import re
import yaml
import threading
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from typing import Optional, Tuple
# ---- GLOBAL.end ----

# ---- DEFINITIONS (Classes & Functions) ----
def safe_print(*args_print, **kwargs):
    """Thread-safe printing to prevent garbled CLI output."""
    with print_lock:
        print(*args_print, **kwargs)

def extract_markdown_code(raw_output: str) -> str:
    """Extracts Python code, stripping markdown code blocks."""
    raw_output = raw_output.strip()
    pattern = r"^```(?:python)?\s*\n(.*?)\n```$"
    match = re.search(pattern, raw_output, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    
    if raw_output.startswith("```python"): raw_output = raw_output[9:]
    elif raw_output.startswith("```"): raw_output = raw_output[3:]
    if raw_output.endswith("```"): raw_output = raw_output[:-3]
        
    return raw_output.strip()

def get_body_lines(script_path: str, lineno: int, end_lineno: int) -> str:
    """Retrieves the full body natively using Python."""
    if lineno == 0 or end_lineno == 0:
        return ""
    try:
        with open(script_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # Python is 0-indexed, AST lineno is 1-indexed
            return "".join(lines[lineno - 1 : end_lineno])
    except Exception as e:
        safe_print(f"[!] Error reading file: {e}")
        return ""

def resolve_component_by_line(raw_ast_data: dict, target_line: int) -> str:
    """Finds the component name based on a given line number from the AST data."""
    # Check top-level functions
    for func_name, func_data in raw_ast_data.get('FunctionDef', {}).items():
        if func_data.get('lineno', 0) <= target_line <= func_data.get('end_lineno', 0):
            return func_name

    # Check top-level classes
    for cls_name, cls_data in raw_ast_data.get('ClassDef', {}).items():
        if cls_data.get('lineno', 0) <= target_line <= cls_data.get('end_lineno', 0):
            return cls_name

    # Check methods within classes (if nested by AST extractor)
    for cls_name, cls_data in raw_ast_data.get('ClassDef', {}).items():
        for method_name, method_data in cls_data.get('FunctionDef', {}).items():
            if method_data.get('lineno', 0) <= target_line <= method_data.get('end_lineno', 0):
                return method_name

    # If the line does not fall inside any function or class definitions, it belongs to the module
    return 'Module'

def get_component_code(raw_ast_data: dict, comp_name: str, script_path: str) -> Tuple[str, str, str]:
    """Resolves the component name to its source code using the AST data."""
    if comp_name.lower() == 'module':
        if 'Module' in raw_ast_data and 'body' in raw_ast_data['Module']:
            return 'Module', 'Module', raw_ast_data['Module']['body']
        return 'Module', 'Module', ''

    funcs = raw_ast_data.get('FunctionDef', {})
    if comp_name in funcs:
        comp = funcs[comp_name]
        return 'FunctionDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    classes = raw_ast_data.get('ClassDef', {})
    if comp_name in classes:
        comp = classes[comp_name]
        return 'ClassDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    for cls_name, cls_data in classes.items():
        methods = cls_data.get('FunctionDef', {})
        if comp_name in methods:
            comp = methods[comp_name]
            return 'FunctionDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    return 'Unknown', comp_name, ""

def execute_worker_agent(host: str, port: int, component_code: str, history: str, user_prompt: str, is_module: bool, debug: bool = False) -> str:
    """Executes the microtask explicitly targeting the loaded component code."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent.\n"
        "You will be given a target component's source code, a history of user prompts, and a new user request.\n"
        "Your job is to EXECUTE the task and return the EXACT updated raw target code.\n\n"
        "RULES:\n"
        "1. Output ONLY the fully updated Python code enclosed in a ```python ... ``` markdown block.\n"
        "2. Do NOT include explanations, introductions, conversational filler, or any other text.\n"
        "3. Maintain original indentation and syntax as closely as possible.\n"
    )

    if is_module:
        system_prompt = system_prompt.replace("target component's source code", "top-level module body")
        
    micro_report = (
        f"### Target Component\n"
        f"- **Change requirement**: {user_prompt}\n"
        f"- **Prompt History**:\n{history if history else 'None'}\n"
        f"- **target**:\n"
        f"```python\n"
        f"{component_code}\n"
        f"```"
    )
    
    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": micro_report}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        if debug:
            safe_print(f"\n[DEBUG] === RAW WORKER RESPONSE ===\n{raw_output}\n[DEBUG] ===========================\n")
            
        return extract_markdown_code(raw_output)
    except Exception as e:
        safe_print(f"       [!] Worker LLM Error: {e}")
        return ""

def model_name(host: str, ports: str, endpoint: str) -> Optional[str]:
    """Fetches the model name from the LLM server endpoint."""
    main_port = str(ports).split(',')[0].strip()
    url = f'http://{host}:{main_port}{endpoint}'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if 'models' in data and data['models']:
            return data['models'][0].get('name')
    except Exception as e:
        safe_print(f'\n❌ ERROR connecting to LLM: {e}')
    return None

def main():
    parser = argparse.ArgumentParser(description='Singletasker Vibe Coding Shell')
    parser.add_argument('-f', '--script', required=True, help='Path to Python script target')
    parser.add_argument('-l', '--line', type=int, required=True, help='Line number within the target component to edit')
    parser.add_argument('--host', default=os.getenv('HOST', 'localhost'), help='LLM Server Host')
    parser.add_argument('--port', default=os.getenv('PORT', '8080'), help='LLM Server Port')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()

    script_path = os.path.abspath(args.script)
    if not os.path.exists(script_path):
        print(f"[!] Script file '{script_path}' not found.")
        return

    base_dir = os.path.dirname(script_path)
    script_name = os.path.basename(script_path)
    module_name = os.path.splitext(script_name)[0]
    meta_dir = os.path.join(base_dir, f".{script_name}")
    ast_file = os.path.join(meta_dir, "ast.yaml")

    # The AST must exist to resolve lines to components
    if not os.path.exists(ast_file):
        print(f"[!] AST file missing. Please generate it first (e.g., run `make ast SCRIPT={args.script}`).")
        return

    with open(ast_file, 'r', encoding='utf-8') as f:
        raw_ast_data = yaml.safe_load(f) or {}

    # 1. Resolve the line number to a component name
    target_comp_name = resolve_component_by_line(raw_ast_data, args.line)
    
    # 2. Extract initial target metadata based on AST mapping
    comp_type, resolved_name, _ = get_component_code(raw_ast_data, target_comp_name, script_path)
    
    if comp_type == 'Unknown':
        print(f"[!] Could not locate component for line {args.line}. Evaluated name: '{target_comp_name}'.")
        return

    is_module = (comp_type == 'Module')
    
    # Generate target path identical to microtasker.py so merge detects the changed module
    if is_module:
        target_path = os.path.join(meta_dir, f"Module.{module_name}")
    else:
        target_path = os.path.join(meta_dir, f"{comp_type}.{resolved_name}")

    MODEL_NAME = model_name(args.host, args.port, endpoint='/models')
    if MODEL_NAME: 
        safe_print(f'✅ Ready: {MODEL_NAME}')

    # Prompt toolkit UI setup
    promptia_session = PromptSession()
    promptia_style = Style.from_dict({'llm': 'bg:#408175 fg:#89D7B7 bold', 'prompt': 'bg:#000000 fg:#89D7B7', 'ws': 'bg:#89D7B7 fg:#89D7B7'})
    
    print(f'==========================================')
    print(f' Singletasker Interactive Shell')
    print(f' Target Line: {args.line} -> Detected Component: {resolved_name} ({comp_type})')
    print(f' Script: {args.script}')
    print(f' Server: {args.host}:{args.port}')
    print(f'==========================================')
    print('Press [Meta+Enter] or [Esc] then [Enter] to submit multiline prompts.')
    print("Type 'exit' or press Ctrl+C to quit.")
    print("Type '===m' to merge current meta changes into the original script.\n")
    
    chat_history = ""

    while True:
        try:
            # Interactive prompt entry
            user_input = promptia_session.prompt(
                [('class:llm', ' SingleTASKER '), ('class:prompt', f' {resolved_name} '), ('class:ws', ' ')], 
                multiline=True, style=promptia_style
            ).strip()
            
            if not user_input: 
                continue
                
            if user_input.lower() in ('exit', 'quit'):
                print('Exiting singletasker shell.')
                break
                
            # Intercept the merge command
            if user_input.strip() == '===m':
                print(f"[*] Triggering make merge for {args.script}...")
                subprocess.run(['make', 'merge', f'SCRIPT={args.script}'])
                continue

            print(f"[*] Processing update for {resolved_name}...")
            
            # Read the absolute latest draft from the meta directory if it exists
            if os.path.exists(target_path):
                with open(target_path, 'r', encoding='utf-8') as f:
                    current_code = f.read()
            else:
                # Fallback to the original script if the file in the meta repo was cleared
                _, _, current_code = get_component_code(raw_ast_data, target_comp_name, script_path)

            # Send prompt + up-to-date context directly to the worker agent
            updated_code = execute_worker_agent(
                host=args.host,
                port=int(str(args.port).split(',')[0]),
                component_code=current_code,
                history=chat_history,
                user_prompt=user_input,
                is_module=is_module,
                debug=args.debug
            )

            if updated_code:
                # Write the new output file exactly where `merge.py` expects it
                with open(target_path, 'w', encoding='utf-8') as f:
                    f.write(updated_code + "\n")
                    
                print(f"[+] Component updated successfully -> {target_path}")
                
                # Render the code back for the user to review
                print(f"\n\033[36m---- CODE PREVIEW ({resolved_name}) ----")
                print(updated_code)
                print(f"\033[36m------------------------------------------\033[0m\n")
                print(f"[*] Type '===m' to merge these changes, or enter a new prompt to keep refining.")
                
                # Add human prompt to session history
                chat_history += f"User: {user_input}\n"
            else:
                print("[-] Failed to update component. Check debug logs.")
        
        except (KeyboardInterrupt, EOFError):
            print('\nExiting singletasker shell.')
            break
# ---- DEFINITIONS.end ----

# ---- MAIN ---
print_lock = threading.Lock()
if __name__ == '__main__':
    main()
# ---- MAIN.end ----
