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
# Utility: Clean Outputs & AWK Command
# ---------------------------------------------------------
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


def  get_body_lines(script_path: str, lineno: int, end_lineno: int) -> str:
    """Retrieves the full body natively using Python."""
    if lineno == 0 or end_lineno == 0:
        return ""
        
    try:
        with open(script_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # Python is 0-indexed, awk's NR is 1-indexed
            return "".join(lines[lineno - 1 : end_lineno])
    except Exception as e:
        print(f"[!] Error reading file: {e}")
        return ""

# ---------------------------------------------------------
# Markdown Formatting Utilities
# ---------------------------------------------------------
def build_markdown_report(raw_ast_data: dict, module_name: str) -> str:
    """Converts the raw architecture dictionary into a strict Markdown report."""
    lines = [f"# Architecture Report: {module_name}", "## Definition"]
    
    arch = raw_ast_data.get('Architecture', {})
    defs = arch.get('definition', {})
    
    # Helper to safely process both dictionaries and lists of dictionaries
    def extract_items(data_node):
        if isinstance(data_node, dict):
            return data_node.items()
        elif isinstance(data_node, list):
            items = []
            for item in data_node:
                if isinstance(item, dict):
                    # Handle flat lists like: - name: my_func \n description: ...
                    if 'name' in item:
                        name = item.pop('name')
                        items.append((name, item))
                    # Handle nested lists like: - my_func: { description: ... }
                    else:
                        items.extend(item.items())
            return items
        return []

    # Process Functions
    lines.append("### Function")
    functions = defs.get('functions', [])
    func_items = extract_items(functions)
    if func_items:
        for name, data in func_items:
            desc = data.get('description', 'No description provided') if isinstance(data, dict) else str(data)
            lines.append(f"- **{name}** : {desc}")
    else:
        lines.append("- *No functions defined*")
        
    # Process Classes
    lines.append("### Class")
    classes = defs.get('class', [])
    cls_items = extract_items(classes)
    if cls_items:
        for name, data in cls_items:
            desc = data.get('description', 'No description provided') if isinstance(data, dict) else str(data)
            lines.append(f"- **{name}**: {desc}")
    else:
        lines.append("- *No classes defined*")
        
    # Process Workflow
    lines.append("## Workflow")
    workflow = arch.get('workflow', {})
    if workflow:
        if isinstance(workflow, dict):
            for k, v in workflow.items():
                lines.append(f"{k}: {v}")
        elif isinstance(workflow, list):
            for item in workflow:
                lines.append(f"- {str(item)}")
        else:
            lines.append(str(workflow))
    else:
        lines.append("*No workflow description provided.*")
        
    return "\n".join(lines)


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
# LLM Integration: Microtask Generation (Architect)
# ---------------------------------------------------------
def compose_microtasks_with_llm(host: str, port: int, report_content: str, user_request: str, debug: bool = False) -> str:
    """Reads the multi-document Markdown architecture report and generates Markdown task instructions."""
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

    user_prompt = (
        "---\n"
        f"{report_content}\n"
        "---\n"
        "# User intent\n"
        f"{user_request}\n"
        "---"
    )

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 4096
    }

    print("[*] Asking Architect LLM to generate microtasks based on the Markdown report...")
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
            print("\n       [DEBUG] === RAW WORKER LLM RESPONSE ===")
            print(f"       {raw_output.replace('\n', '\n       ')}")
            print("       [DEBUG] ===============================\n")
            
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
            print("\n       [DEBUG] === RAW MODULE BODY WORKER LLM RESPONSE ===")
            print(f"       {raw_output.replace('\n', '\n       ')}")
            print("       [DEBUG] ===========================================\n")
            
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

def get_component_code(raw_ast_data: dict, comp_name: str, script_path: str) -> tuple[str, str, str]:
    """Resolves the component name to its source code using the AST data."""
    if comp_name.lower() == 'workflow' or comp_name.lower() == 'module':
        if 'Module' in raw_ast_data and 'body' in raw_ast_data['Module']:
            return 'Module', 'Module', raw_ast_data['Module']['body']
        return 'Module', 'Module', ''

    # Check top-level functions
    funcs = raw_ast_data.get('FunctionDef', {})
    if comp_name in funcs:
        comp = funcs[comp_name]
        return 'FunctionDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    # Check top-level classes
    classes = raw_ast_data.get('ClassDef', {})
    if comp_name in classes:
        comp = classes[comp_name]
        return 'ClassDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    # Check methods within classes
    for cls_name, cls_data in classes.items():
        methods = cls_data.get('FunctionDef', {})
        if comp_name in methods:
            comp = methods[comp_name]
            return 'FunctionDef', comp_name, get_body_lines(script_path, comp.get('lineno', 0), comp.get('end_lineno', 0))

    return 'Unknown', comp_name, ""

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

    # Build the multi-document Markdown report
    report_content = build_markdown_report(raw_ast_data, module_name)

    # 1. Compose Microtasks using Architect
    llm_response = compose_microtasks_with_llm(args.host, args.port, report_content, args.prompt, args.debug)
    
    # 2. Extract tasks using Regex
    tasks = extract_markdown_microtasks(llm_response)
    if not tasks:
        print("[!] No valid microtasks generated.")
        return

    print(f"\n[*] Generated {len(tasks)} microtasks. Starting Worker Agent Loop...")
    
    # 3. Execute Microtasks (Agent Loop)
    for i, task in enumerate(tasks, 1):
        comp_name = task['component_name']
        comp_type, resolved_name, component_code = get_component_code(raw_ast_data, comp_name, script_path)
        micro_report_md = format_worker_report(task, component_code)
        
        print(f"    -> Executing Task {i}/{len(tasks)} targeting: {resolved_name}...")
        print("       [*] Microtask Report Outline:")
        print(f"           \033[38;5;126m{task['requirement']}{RESET}")
        
        # Route to the appropriate micro-agent
        if comp_type == 'Module':
            print("       [~] Routing to dedicated Module Body Agent...")
            updated_code = execute_module_body_agent(args.host, args.port, micro_report_md, args.debug)
            target_path = os.path.join(meta_dir, f"Module.{module_name}")
        else:
            updated_code = execute_worker_agent(args.host, args.port, micro_report_md, args.debug)
            target_path = os.path.join(meta_dir, f"{comp_type}.{resolved_name}")
        
        # Export the updated code directly to the meta directory
        if updated_code:
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(updated_code + "\n")
            print(f"       [+] Process complete. Wrote updated component -> {target_path}")
        else:
            print(f"       [-] Worker failed to update component: `{comp_type}.{resolved_name}`.")
            if not args.debug:
                print("       [!] HINT: Run the command with `DEBUG=true` (or pass `--debug`) to see the RAW LLM output.")

if __name__ == "__main__":
    main()
