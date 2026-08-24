#!/usr/bin/env python
import argparse
import ast
import os
import requests
import re
import yaml
import subprocess
from typing import Optional, Dict, List, Tuple

# ---------------------------------------------------------
# PyYAML Configuration (Preserve Multiline Strings as | )
# ---------------------------------------------------------
def str_presenter(dumper, data):
    if len(str(data).splitlines()) > 1:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)

yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)

# ---------------------------------------------------------
# Utility: Clean Outputs & Strict AST Diffing
# ---------------------------------------------------------
def extract_markdown_code(raw_output: str) -> str:
    """Extracts Python code, stripping markdown code blocks."""
    raw_output = raw_output.strip()
    pattern = r"^```(?:python)?\s*\n(.*?)\n```"
    match = re.search(pattern, raw_output, re.DOTALL | re.IGNORECASE | re.MULTILINE)
    
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
            return "".join(lines[lineno - 1 : end_lineno])
    except Exception as e:
        print(f"[!] Error reading file: {e}")
        return ""

def get_strict_args(node) -> List[str]:
    """Extracts strictly ordered arguments with their type annotations."""
    args = []
    
    def format_arg(a):
        if getattr(a, 'annotation', None):
            return f"{a.arg}: {ast.unparse(a.annotation)}"
        return a.arg

    if getattr(node.args, 'posonlyargs', None):
        args.extend(format_arg(a) for a in node.args.posonlyargs)
    if getattr(node.args, 'args', None):
        args.extend(format_arg(a) for a in node.args.args)
    if getattr(node.args, 'vararg', None):
        args.append(f"*{format_arg(node.args.vararg)}")
    if getattr(node.args, 'kwonlyargs', None):
        args.extend(format_arg(a) for a in node.args.kwonlyargs)
    if getattr(node.args, 'kwarg', None):
        args.append(f"**{format_arg(node.args.kwarg)}")
    
    return args

def parse_component_signature(source: str, comp_name: str) -> tuple[Optional[list], Optional[str]]:
    """Deterministically extracts the strict signature (order, types) of a function/method."""
    try:
        tree = ast.parse(source)
    except Exception:
        return None, None
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == comp_name or ('.' in comp_name and comp_name.endswith(node.name)):
                in_args = get_strict_args(node)
                out_type = ast.unparse(node.returns) if getattr(node, 'returns', None) else "None"
                return in_args, out_type
                
    return None, None

def get_original_component_code(raw_ast_data: dict, comp_name: str, script_path: str) -> str:
    """Resolves the original component code from the base script, ignoring WIP drafts."""
    if comp_name.lower() == 'workflow' or comp_name.lower() == 'module':
        return raw_ast_data.get('Module', {}).get('body', '')

    funcs = raw_ast_data.get('FunctionDef', {})
    classes = raw_ast_data.get('ClassDef', {})
    
    if comp_name in funcs:
        return get_body_lines(script_path, funcs[comp_name].get('lineno', 0), funcs[comp_name].get('end_lineno', 0))
    elif comp_name in classes:
        return get_body_lines(script_path, classes[comp_name].get('lineno', 0), classes[comp_name].get('end_lineno', 0))
    else:
        for cls_name, cls_data in classes.items():
            methods = cls_data.get('FunctionDef', {})
            if comp_name in methods:
                return get_body_lines(script_path, methods[comp_name].get('lineno', 0), methods[comp_name].get('end_lineno', 0))
    return ""

# ---------------------------------------------------------
# Markdown Formatting Utilities
# ---------------------------------------------------------
def build_markdown_report(raw_ast_data: dict, module_name: str) -> str:
    """Converts the raw architecture dictionary into a strict Markdown report."""
    lines = [f'# Architecture Report: {module_name}', '## Definition']
    arch = raw_ast_data.get('Architecture', {})
    defs = arch.get('definition', {})

    def extract_items(data_node):
        if isinstance(data_node, dict):
            return data_node.items()
        elif isinstance(data_node, list):
            items = []
            for item in data_node:
                if isinstance(item, dict):
                    if 'name' in item:
                        name = item.pop('name')
                        items.append((name, item))
                    else:
                        items.extend(item.items())
            return items
        return []
        
    lines.append('### Function')
    functions = defs.get('functions', [])
    func_items = extract_items(functions)
    if func_items:
        for name, data in func_items:
            desc = data.get('description', 'No description provided') if isinstance(data, dict) else str(data)
            lines.append(f'- **{name}** : {desc}')
    else:
        lines.append('- *No functions defined*')
        
    lines.append('### Class')
    classes = defs.get('class', [])
    cls_items = extract_items(classes)
    if cls_items:
        for name, data in cls_items:
            desc = data.get('description', 'No description provided') if isinstance(data, dict) else str(data)
            lines.append(f'- **{name}**: {desc}')
    else:
        lines.append('- *No classes defined*')
        
    lines.append('## Workflow')
    workflow = arch.get('workflow', {})
    if workflow:
        if isinstance(workflow, dict):
            for k, v in workflow.items():
                lines.append(f'{k}: {v}')
        elif isinstance(workflow, list):
            for item in workflow:
                lines.append(f'- {str(item)}')
        else:
            lines.append(str(workflow))
    else:
        lines.append('*No workflow description provided.*')

    lines.append('## Dataflow')
    dataflow = arch.get('dataflow', {})
    if dataflow:
        if 'MODULE' in dataflow:
            lines.append('### MODULE')
            calls = dataflow['MODULE'].get('calls', [])
            if calls:
                lines.append('- calls:')
                for call in calls:
                    lines.append(f'  - {call}')
            else:
                lines.append('- calls: []')
        
        for comp_name, data in dataflow.items():
            if comp_name == 'MODULE': continue
            lines.append(f'### {comp_name}')
            in_args = data.get('in', [])
            in_str = f"[{', '.join(in_args)}]" if isinstance(in_args, list) else str(in_args)
            lines.append(f'- in: {in_str}')
            out_val = data.get('out', 'None')
            lines.append(f'- out: {out_val}')
            calls = data.get('calls', [])
            if calls:
                lines.append('- call:')
                for call in calls:
                    lines.append(f'  - {call}')
            else:
                lines.append('- call: []')
    else:
        lines.append('*No dataflow definitions provided.*')

    return '\n'.join(lines)

def format_worker_report(task: dict, component_code: str) -> str:
    """Formats the final Markdown Microtask Report for the worker agent."""
    return (
        f"### {task['component_name']}\n"
        f"- **Change requirement**: {task['requirement']}\n"
        f"- **Implementation Hints**: {task['hints']}\n"
        f"- **target**:\n"
        f"```python\n"
        f"{component_code}\n"
        f"```"
    )

# ---------------------------------------------------------
# LLM Integration: Architect Generation & Review
# ---------------------------------------------------------
def compose_microtasks_with_llm(host: str, port: int, report_content: str, user_request: str, debug: bool = False) -> str:
    """Reads the architecture report and generates initial Markdown task instructions."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are a master Software Architect. You are working with a single Python module and its internal components.\n"
        "You will receive a multi-document report containing an 'Architecture Report' detailing the module's structure, and a 'User intent', separated by '---' dividers.\n"
        "Your goal is to analyze the user intent and determine exactly which components within this module need to be modified.\n\n"
        "RULES:\n"
        "1. Pick the exact component names from the Architecture Report (e.g., function names, class names, or 'Workflow') that must be changed to meet the user's intent.\n"
        "2. For each component you pick, output the directions STRICTLY in the following Markdown format. Do not add any conversational text outside of this structure:\n\n"
        "### <component_name>\n"
        "- **Change requirement**: <detailed objective and expected inputs/outputs>\n"
        "- **Implementation Hints**: <tailored hints for the component>\n"
    )

    user_prompt = f"---\n{report_content}\n---\n# User intent\n{user_request}\n---"

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 4096
    }

    print("[*] Asking Architect LLM to generate initial microtasks...")
    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        if debug:
            print("\n[DEBUG] === RAW ARCHITECT RESPONSE ===\n" + raw_output + "\n[DEBUG] ==============================\n")
        return raw_output
    except requests.exceptions.RequestException as e:
        print(f"[!] Architect LLM Connection Error: {e}")
        return ""

def review_interface_deltas_with_llm(host: str, port: int, report_content: str, deltas: str, debug: bool = False) -> str:
    """Reviews explicitly detected interface changes to generate tasks for affected downstream callers."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are a master Software Architect overseeing an automated code modification pipeline.\n"
        "Worker agents have modified some components, and a strict programmatic AST analysis has detected that their interface signatures (argument order, type annotations, or returns) have changed.\n"
        "You will be provided with the 'Architecture Report' (showing dataflow and structure) and the 'Interface Deltas' (listing the exact old/new signatures and any dependent caller components).\n"
        "Your ONLY job is to determine if these strict signature changes break the Dependent Callers.\n\n"
        "RULES:\n"
        "1. If a Dependent Caller needs to be updated to accommodate the new signature, compose a new microtask for it.\n"
        "2. Output new tasks STRICTLY in this Markdown format:\n\n"
        "### <component_name>\n"
        "- **Change requirement**: <detailed objective to update the component for the newly modified interface>\n"
        "- **Implementation Hints**: <hints>\n\n"
        "3. If no dependent callers need updates, or there are no dependent callers, output the exact word: APPROVED\n"
        "Do NOT add any conversational text."
    )

    user_prompt = f"---\n{report_content}\n---\n# Interface Deltas\n{deltas}"

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        if debug:
            print("\n[DEBUG] === ARCHITECT DELTA REVIEW RESPONSE ===\n" + raw_output + "\n[DEBUG] =======================================\n")
        return raw_output
    except requests.exceptions.RequestException as e:
        print(f"[!] Architect LLM Review Error: {e}")
        return ""

# ---------------------------------------------------------
# LLM Integration: Worker Agents (Executors)
# ---------------------------------------------------------
def model_name(host: str, ports: str, endpoint: str) -> Optional[str]:
    main_port = str(ports).split(',')[0].strip()
    url = f"http://{host}:{main_port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if 'models' in data and data['models']:
            return data['models'][0].get('name')
    except Exception as e:
        print(f"\n❌ ERROR connecting to LLM: {e}")
    return None

def execute_worker_agent(host: str, port: int, micro_report_md: str, debug: bool = False) -> str:
    """Executes code changes strictly returning code."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent.\n"
        "You will be given a microtask containing change requirements, hints, and the target source code.\n"
        "Your job is to EXECUTE the task and return the EXACT updated raw target code so it can be saved directly into the component file.\n\n"
        "RULES:\n"
        "1. Output ONLY the fully updated Python code enclosed in a ```python ... ``` markdown block.\n"
        "2. Do NOT include explanations, introductions, conversational filler, or any other text.\n"
        "3. Maintain original indentation and syntax as closely as possible.\n"
    )

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": micro_report_md}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        if debug:
            print("\n       [DEBUG] === RAW WORKER LLM RESPONSE ===\n" + raw_output + "\n       [DEBUG] ===============================\n")
            
        return extract_markdown_code(raw_output)
    except Exception as e:
        print(f"       [!] Worker LLM Error: {e}")
        return ""

def execute_module_body_agent(host: str, port: int, micro_report_md: str, debug: bool = False) -> str:
    """Dedicated micro-agent explicitly for handling modifications to the top-level Module body."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent specializing in top-level module code.\n"
        "You will be given a microtask containing a requirement and the top-level body of the script.\n"
        "Your job is to EXECUTE the task and return the EXACT updated raw target code so it can be saved directly into the file.\n\n"
        "RULES:\n"
        "1. Output ONLY the fully updated Python code enclosed in a ```python ... ``` markdown block.\n"
        "2. Do NOT include explanations, introductions, conversational filler, or any other text.\n"
    )

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": micro_report_md}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        if debug:
            print("\n       [DEBUG] === RAW MODULE WORKER LLM RESPONSE ===\n" + raw_output + "\n       [DEBUG] =======================================\n")
            
        return extract_markdown_code(raw_output)
    except Exception as e:
        print(f"       [!] Module Body Worker LLM Error: {e}")
        return ""

# ---------------------------------------------------------
# Memory Management & Microtask Extraction
# ---------------------------------------------------------
def extract_markdown_microtasks(llm_output: str) -> list:
    """Parses the Markdown output from the Architect LLM into task dictionaries."""
    pattern = r"###\s+([^\n]+)\n-\s+\*\*Change requirement\*\*:\s*(.*?)\n-\s+\*\*Implementation Hints\*\*:\s*(.*?)(?=\n###|\Z)"
    matches = re.findall(pattern, llm_output, re.DOTALL | re.IGNORECASE)
    
    tasks = []
    for match in matches:
        tasks.append({
            'component_name': match[0].strip(),
            'requirement': match[1].strip(),
            'hints': match[2].strip()
        })
    return tasks

def get_component_code(raw_ast_data: dict, comp_name: str, script_path: str, meta_dir: str, module_name: str) -> tuple[str, str, str]:
    """Resolves the component code. It reads WIP drafts from the meta_dir if they exist."""
    comp_type = 'Unknown'
    resolved_name = comp_name
    base_code = ""

    if comp_name.lower() == 'workflow' or comp_name.lower() == 'module':
        comp_type = 'Module'
        resolved_name = 'Module'
        if 'Module' in raw_ast_data and 'body' in raw_ast_data['Module']:
            base_code = raw_ast_data['Module']['body']
    else:
        funcs = raw_ast_data.get('FunctionDef', {})
        classes = raw_ast_data.get('ClassDef', {})
        
        if comp_name in funcs:
            comp_type = 'FunctionDef'
            base_code = get_body_lines(script_path, funcs[comp_name].get('lineno', 0), funcs[comp_name].get('end_lineno', 0))
        elif comp_name in classes:
            comp_type = 'ClassDef'
            base_code = get_body_lines(script_path, classes[comp_name].get('lineno', 0), classes[comp_name].get('end_lineno', 0))
        else:
            for cls_name, cls_data in classes.items():
                methods = cls_data.get('FunctionDef', {})
                if comp_name in methods:
                    comp_type = 'FunctionDef'
                    base_code = get_body_lines(script_path, methods[comp_name].get('lineno', 0), methods[comp_name].get('end_lineno', 0))

    # Read from WIP draft if already modified in a previous iteration
    if comp_type != 'Unknown':
        target_path = os.path.join(meta_dir, f"Module.{module_name}") if comp_type == 'Module' else os.path.join(meta_dir, f"{comp_type}.{resolved_name}")
        if os.path.exists(target_path):
            with open(target_path, 'r', encoding='utf-8') as f:
                return comp_type, resolved_name, f.read()

    return comp_type, resolved_name, base_code

# ---------------------------------------------------------
# CLI & Main Loop
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Microtask Composer & Execution Loop")
    parser.add_argument('--port', type=str, default='8080', help='Port(s) for the local LLM API (e.g. 8080,8085)')
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--script', type=str, required=True, help='Path to the original Python script')
    parser.add_argument('--prompt', type=str, required=True, help='User intent/request for updating the codebase')
    parser.add_argument('--debug', action='store_true', help='Print verbose outputs')
    
    args = parser.parse_args()
    
    port_str = str(args.port)
    if ',' in port_str:
        main_port = int(port_str.split(',')[0].strip())
        worker_port = int(port_str.split(',')[1].strip())
    else:
        main_port = int(port_str)
        worker_port = main_port

    script_path = os.path.abspath(args.script)
    if not os.path.exists(script_path):
        print(f"[!] Script file '{script_path}' not found.")
        return

    base_dir = os.path.dirname(script_path)
    script_name = os.path.basename(script_path)
    module_name = os.path.splitext(script_name)[0]
    meta_dir = os.path.join(base_dir, f".{script_name}")
    
    ast_file = os.path.join(meta_dir, "ast.yaml")
    archer_file = os.path.join(meta_dir, "archer.yaml")
    
    RESET="\033[0m"
    if not os.path.exists(ast_file) or not os.path.exists(archer_file):
        print(f"[!] Missing necessary files. Checked {ast_file} and {archer_file}.")
        return

    raw_ast_data = {}
    
    with open(ast_file, 'r', encoding='utf-8') as f:
        ast_data = yaml.safe_load(f)
        if ast_data: raw_ast_data.update(ast_data)
        
    with open(archer_file, 'r', encoding='utf-8') as f:
        archer_data = yaml.safe_load(f)
        if archer_data: raw_ast_data.update(archer_data)

    if 'Architecture' not in raw_ast_data:
        print("[!] Cannot find a valid 'Architecture' key in the combined reports.")
        return

    report_content = build_markdown_report(raw_ast_data, module_name)

    llm_response = compose_microtasks_with_llm(args.host, main_port, report_content, args.prompt, args.debug)
    tasks = extract_markdown_microtasks(llm_response)
    
    if not tasks:
        print("[!] No valid microtasks generated.")
        return

    WORKER_MODEL_NAME = model_name(args.host, worker_port, endpoint="/models")
    if WORKER_MODEL_NAME:
        print("[%] LLM Worker begins")
        print(f"    ✅ Ready: {WORKER_MODEL_NAME}")

    iteration = 1
    max_iterations = 4
    max_worker_retries = 3

    # The Deterministic Feedback Loop
    while tasks and iteration <= max_iterations:
        print(f"\n[*] Execution Cycle {iteration} | Targeting {len(tasks)} components...")
        deltas_found = []

        for i, task in enumerate(tasks, 1):
            comp_name = task['component_name']
            comp_type, resolved_name, component_code = get_component_code(raw_ast_data, comp_name, script_path, meta_dir, module_name)
            micro_report_md = format_worker_report(task, component_code)
            
            print(f"    -> Task {i}/{len(tasks)}: {resolved_name}...")
            print(f"       [*] \033[38;5;126m{task['requirement']}{RESET}")
            
            updated_code = ""
            
            # Retry loop for worker agent execution
            for attempt in range(1, max_worker_retries + 1):
                if comp_type == 'Module':
                    updated_code = execute_module_body_agent(args.host, worker_port, micro_report_md, args.debug)
                    target_path = os.path.join(meta_dir, f"Module.{module_name}")
                else:
                    updated_code = execute_worker_agent(args.host, worker_port, micro_report_md, args.debug)
                    target_path = os.path.join(meta_dir, f"{comp_type}.{resolved_name}")
                
                if updated_code:
                    with open(target_path, 'w', encoding='utf-8') as f:
                        f.write(updated_code + "\n")
                    print(f"       [+] Wrote updated code -> {target_path}")
                    break
                else:
                    if attempt < max_worker_retries:
                        print(f"       [-] Worker returned empty. Retrying ({attempt}/{max_worker_retries})...")
                    else:
                        print(f"       [-] Worker failed to update component: `{comp_type}.{resolved_name}` after {max_worker_retries} attempts.")

            # Run strict deterministic AST Delta check if it's a function/method
            if updated_code and comp_type in ['FunctionDef', 'ClassDef']:
                new_in, new_out = parse_component_signature(updated_code, resolved_name)
                
                # Fetch the true original source for accurate strict signature comparison
                original_code = get_original_component_code(raw_ast_data, resolved_name, script_path)
                old_in, old_out = parse_component_signature(original_code, resolved_name)
                
                if new_in is not None and old_in is not None and (old_in != new_in or old_out != new_out):
                    # Extract callers that depend on this component
                    callers = []
                    dataflow = raw_ast_data.get('Architecture', {}).get('dataflow', {})
                    for caller_name, caller_data in dataflow.items():
                        if resolved_name in caller_data.get('calls', []):
                            callers.append(caller_name)
                    
                    delta_info = (
                        f"### {resolved_name}\n"
                        f"- Old Signature: in={old_in}, out={old_out}\n"
                        f"- New Signature: in={new_in}, out={new_out}\n"
                        f"- Dependent Callers: {callers if callers else 'None'}\n"
                    )
                    deltas_found.append(delta_info)
                    print(f"       [!] Strict interface modification detected (Arguments/Types changed). Alerting Architect.")
                else:
                    print(f"       [✓] Strict interface verified intact. No delta detected.")

        if not deltas_found:
            print("\n[*] No interface signature changes detected. Bypassing Architect review.")
            break

        print(f"\n[*] Reviewing {len(deltas_found)} interface delta(s) with LLM Architect...")
        deltas_text = "\n".join(deltas_found)
        review_response = review_interface_deltas_with_llm(args.host, main_port, report_content, deltas_text, args.debug)
        
        if "APPROVED" in review_response.upper() and not extract_markdown_microtasks(review_response):
            print("    [+] Architect approved the new interface structure. No downstream updates needed.")
            break
            
        new_tasks = extract_markdown_microtasks(review_response)
        if new_tasks:
            print(f"    [!] Architect deployed {len(new_tasks)} new microtasks to handle interface dependencies.")
            tasks = new_tasks
        else:
            print("    [?] Architect did not explicitly approve, but provided no new tasks. Concluding pipeline.")
            break
            
        iteration += 1

    if iteration > max_iterations:
        print("[!] Reached maximum iteration limit for refinement.")

if __name__ == "__main__":
    main()
