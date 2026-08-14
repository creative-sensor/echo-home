#!/usr/bin/env python
import argparse
import os
import requests
import re
import yaml
import subprocess

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
# Utility: Clean Outputs
# ---------------------------------------------------------
def clean_yaml_output(raw_output: str) -> str:
    """Extracts YAML content, strictly stripping markdown code blocks if present."""
    raw_output = raw_output.strip()
    pattern = r"^```(?:yaml)?\s*\n(.*?)\n```$"
    match = re.search(pattern, raw_output, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    
    if raw_output.startswith("```yaml"): raw_output = raw_output[7:]
    elif raw_output.startswith("```"): raw_output = raw_output[3:]
    if raw_output.endswith("```"): raw_output = raw_output[:-3]
        
    return raw_output.strip()

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

# ---------------------------------------------------------
# Utility: AWK Command
# ---------------------------------------------------------
def get_body_via_awk(script_path: str, lineno: int, end_lineno: int) -> str:
    """Retrieves the full body using awk from the original script."""
    cmd = f"awk 'NR >={lineno} && NR <={end_lineno} ' {script_path}"
    try:
        return subprocess.check_output(cmd, shell=True, text=True)
    except Exception as e:
        print(f"[!] Error running awk: {e}")
        return ""

# ---------------------------------------------------------
# LLM Integration: Microtask Generation (Architect)
# ---------------------------------------------------------
def compose_microtasks_with_llm(host: str, port: int, report_content: str, user_request: str, debug: bool = False) -> str:
    """Reads the high-level YAML architecture report and generates YAML microtasks."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are a master Software Architect. You are given a high-level architectural report of a module (in YAML format) and a user request to update it.\n"
        "Your goal is to analyze the user intent and determine exactly which parts of the module need to be modified.\n\n"
        "RULES:\n"
        "1. Analyze the user intent and pick candidate components for change strictly from these sections in the architecture report:\n"
        "   - Architecture.definition.functions\n"
        "   - Architecture.definition.class\n"
        "   - Architecture.workflow\n"
        "2. Pick the relevant candidate components that must be changed to fulfill the user's intent.\n"
        "3. For EACH distinct component you pick, output a separate, strictly formatted YAML microtask enclosed EXACTLY in [TASK_START] and [TASK_END] tags.\n"
        "4. Inside the tags, provide ONLY valid YAML.\n"
        "5. The YAML MUST start with a 'Task_Requirement' key (as a block scalar `|`) containing the objective, expected inputs/outputs, and implementation hints tailored specifically for that component.\n"
        "6. Immediately following 'Task_Requirement', list the target component structure. Because you are only seeing the high-level view, just output the component type and name with an empty dictionary (`{}`). The system will automatically inject the low-level code before giving it to the worker.\n\n"
        "EXPECTED FORMAT (For Functions):\n"
        "[TASK_START]\n"
        "Task_Requirement: |\n"
        "  - Objective: ...\n"
        "  - Implementation Hints: ...\n"
        "FunctionDef:\n"
        "  <FUNCTION_NAME>: {}\n"
        "[TASK_END]\n\n"
        "EXPECTED FORMAT (For Class Methods):\n"
        "[TASK_START]\n"
        "Task_Requirement: |\n"
        "  - Objective: ...\n"
        "  - Implementation Hints: ...\n"
        "ClassDef:\n"
        "  <CLASS_NAME>:\n"
        "    FunctionDef:\n"
        "      <METHOD_NAME>: {}\n"
        "[TASK_END]\n\n"
        "EXPECTED FORMAT (For Main Module Logic / Architecture.workflow):\n"
        "[TASK_START]\n"
        "Task_Requirement: |\n"
        "  - Objective: ...\n"
        "  - Implementation Hints: ...\n"
        "Module:\n"
        "  body: {}\n"
        "[TASK_END]"
    )

    user_prompt = f"### ARCHITECTURE REPORT (YAML)\n```yaml\n{report_content}\n```\n\n### USER REQUEST\n{user_request}"

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 4096
    }

    print("[*] Asking Architect LLM to generate microtasks based on the high-level report...")
    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        if debug:
            print("\n[DEBUG] === RAW ARCHITECT RESPONSE ===")
            print(raw_output)
            print("[DEBUG] ==============================\n")
            
        return raw_output
    except requests.exceptions.RequestException as e:
        print(f"[!] Architect LLM Connection Error: {e}")
        return ""

# ---------------------------------------------------------
# LLM Integration: Worker Agents (Executors)
# ---------------------------------------------------------
def execute_worker_agent(host: str, port: int, micro_report_md: str, debug: bool = False) -> str:
    """Sends a markdown microtask report to the Worker LLM to modify Python code."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent.\n"
        "You are given a microtask report containing a 'Task_Requirement' and the full raw source code of a target component.\n"
        "Your job is to EXECUTE the task by modifying the provided code.\n\n"
        "RULES:\n"
        "1. Output ONLY the updated Python code enclosed in a ```python ... ``` markdown block.\n"
        "2. Do NOT include explanations, the task requirement, or any other text.\n"
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
        return extract_markdown_code(raw_output)
    except Exception as e:
        print(f"[!] Worker LLM Error: {e}")
        return ""

def execute_module_body_agent(host: str, port: int, micro_report_md: str, debug: bool = False) -> str:
    """Dedicated micro-agent explicitly for handling modifications to the top-level Module body."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent specializing in top-level module code.\n"
        "You are given a microtask report containing a 'Task_Requirement' and the 'Module' component which contains the top-level body of the script.\n"
        "Your job is to EXECUTE the task by modifying the body field code.\n\n"
        "RULES:\n"
        "1. Output ONLY the updated Python code enclosed in a ```python ... ``` markdown block.\n"
        "2. Do NOT include explanations, the task requirement, or any other text.\n"
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
        return extract_markdown_code(raw_output)
    except Exception as e:
        print(f"[!] Module Body Worker LLM Error: {e}")
        return ""

# ---------------------------------------------------------
# Formatting: Markdown Micro Reports 
# ---------------------------------------------------------
def format_micro_report(task: dict) -> tuple[str, str, str]:
    """Converts a task YAML dictionary into a simplified Markdown micro report."""
    req = task.get('Task_Requirement', '').strip()
    
    md = f"### Task_Requirement\n{req}\n"
    comp_type = "Unknown"
    comp_name = "unknown"
    
    for key, val in task.items():
        if key == 'Task_Requirement': continue
        
        if key == 'Module':
            comp_type = key
            comp_name = "Module"
            body = val.get('body', '').strip()
            md += f"### Module\n```python\n{body}\n```\n"
        
        elif key in ['FunctionDef', 'ClassDef'] and isinstance(val, dict):
            for c_name, c_data in val.items():
                comp_type = key
                comp_name = c_name
                body = c_data.get('body', '').rstrip()
                
                md += f"### {key}\n```python\n{body}\n```\n"
                    
    return md, comp_type, comp_name

# ---------------------------------------------------------
# Memory Management & Microtask Extraction
# ---------------------------------------------------------
def find_component_in_raw(raw_ast_data: dict, comp_type: str, comp_name: str) -> dict:
    if comp_type in raw_ast_data and comp_name in raw_ast_data[comp_type]:
        return raw_ast_data[comp_type][comp_name]
    if 'ClassDef' in raw_ast_data:
        for cls_data in raw_ast_data['ClassDef'].values():
            if comp_type in cls_data and comp_name in cls_data[comp_type]:
                return cls_data[comp_type][comp_name]
    return {}

def extract_microtasks(llm_output: str, raw_ast_data: dict, script_path: str) -> list:
    """Parses LLM task output, merges raw AST info in memory, and returns a list of tasks."""
    pattern = r"\[TASK_START\]\n?(.*?)\n?\[TASK_END\]"
    matches = re.findall(pattern, llm_output, re.DOTALL)
    
    tasks = []
    for i, task_content in enumerate(matches, start=1):
        clean_content = clean_yaml_output(task_content)

        try:
            task_yaml = yaml.safe_load(clean_content)
            
            for key, val in task_yaml.items():
                if key == 'Task_Requirement': continue
                
                # Handling special Module body tasks from AST report
                if key == 'Module' and isinstance(val, dict):
                    if val is None:
                        val = {}
                        task_yaml[key] = val
                    if 'Module' in raw_ast_data and 'body' in raw_ast_data['Module']:
                        if 'body' not in task_yaml['Module']:
                            task_yaml['Module']['body'] = ""
                        task_yaml['Module']['body'] = raw_ast_data['Module']['body']
                
                # Functions and Classes using AWK for full code
                elif key in ['FunctionDef', 'ClassDef'] and isinstance(val, dict):
                    for comp_name, comp_data in val.items():
                        if comp_data is None:
                            comp_data = {}
                            val[comp_name] = comp_data

                        raw_comp = find_component_in_raw(raw_ast_data, key, comp_name)
                        if raw_comp:
                            if 'lineno' in raw_comp and 'end_lineno' in raw_comp:
                                comp_data['body'] = get_body_via_awk(script_path, raw_comp['lineno'], raw_comp['end_lineno'])
                                        
            tasks.append(task_yaml)
        except yaml.YAMLError as e:
            print(f"[!] Failed to parse YAML for a task: {e}")
            
    return tasks

# ---------------------------------------------------------
# CLI & Main Loop
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Microtask Composer & Execution Loop")
    parser.add_argument('--port', type=int, default=8080, help='Port for the local LLM API')
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--script', type=str, required=True, help='Path to the original Python script')
    parser.add_argument('--prompt', type=str, required=True, help='User intent/request for updating the codebase')
    parser.add_argument('--debug', action='store_true', help='Print verbose outputs')
    
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
    archer_file = os.path.join(meta_dir, "archer.yaml")
    
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

    filtered_context = {'Architecture': raw_ast_data['Architecture']}
    architect_context_str = yaml.safe_dump(filtered_context, default_flow_style=False, sort_keys=False, width=float("inf"))

    # 1. Compose Microtasks using Architect
    llm_response = compose_microtasks_with_llm(args.host, args.port, architect_context_str, args.prompt, args.debug)
    
    # 2. Extract tasks and inject FULL awk code strings
    tasks = extract_microtasks(llm_response, raw_ast_data, script_path)
    
    if not tasks:
        print("[!] No valid microtasks generated.")
        return

    print(f"\n[*] Generated {len(tasks)} microtasks. Starting Worker Agent Loop...")
    
    # 3. Execute Microtasks (Agent Loop)
    for i, task in enumerate(tasks, 1):
        # Convert the complex YAML task into a simple Markdown Micro Report
        micro_report_md, comp_type, comp_name = format_micro_report(task)
        
        print(f"    -> Executing Task {i}/{len(tasks)}...")
        print("       [*] Microtask Report:")
        print("       " + "-" * 50)
        for line in micro_report_md.splitlines():
            print(f"       {line}")
        print("       " + "-" * 50 + "\n")
        
        # Route to the appropriate micro-agent depending on what component is being targeted
        if comp_type == 'Module':
            print("       [~] Routing to dedicated Module Body Agent...")
            updated_code = execute_module_body_agent(args.host, args.port, micro_report_md, args.debug)
            target_path = os.path.join(meta_dir, f"Module.{module_name}")
        else:
            updated_code = execute_worker_agent(args.host, args.port, micro_report_md, args.debug)
            target_path = os.path.join(meta_dir, f"{comp_type}.{comp_name}")
        
        # Export the updated code directly to the meta directory
        if updated_code:
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(updated_code + "\n")
            print(f"       [+] Process complete. Wrote updated component -> {target_path}")
            
            if args.debug:
                print(f"\n       [DEBUG] Final Task Data after execution:")
                print("       " + "-" * 50)
                print(updated_code)
                print("       " + "-" * 50 + "\n")
        else:
            print("       [-] Worker failed to update component.")

if __name__ == "__main__":
    main()
