#!/usr/bin/env python
import argparse
import os
import requests
import re
import json
import yaml

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
# Utility: Clean YAML Output from LLM
# ---------------------------------------------------------
def clean_yaml_output(raw_output: str) -> str:
    """Extracts YAML content, strictly stripping markdown code blocks if present."""
    raw_output = raw_output.strip()
    
    # Try to extract content inside ```yaml ... ``` or ``` ... ```
    pattern = r"^```(?:yaml)?\s*\n(.*?)\n```$"
    match = re.search(pattern, raw_output, re.DOTALL | re.IGNORECASE)
    
    if match:
        return match.group(1).strip()
    
    # Fallback if the regex fails but it still has partial markdown tags
    if raw_output.startswith("```yaml"): raw_output = raw_output[7:]
    elif raw_output.startswith("```"): raw_output = raw_output[3:]
    if raw_output.endswith("```"): raw_output = raw_output[:-3]
        
    return raw_output.strip()

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
        "  Objective: ...\n"
        "FunctionDef:\n"
        "  <FUNCTION_NAME>: {}\n"
        "[TASK_END]\n\n"
        "EXPECTED FORMAT (For Class Methods):\n"
        "[TASK_START]\n"
        "Task_Requirement: |\n"
        "  Objective: ...\n"
        "ClassDef:\n"
        "  <CLASS_NAME>:\n"
        "    FunctionDef:\n"
        "      <METHOD_NAME>: {}\n"
        "[TASK_END]\n\n"
        "EXPECTED FORMAT (For Main Module Logic / Architecture.workflow):\n"
        "[TASK_START]\n"
        "Task_Requirement: |\n"
        "  Objective: ...\n"
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
def execute_worker_agent(host: str, port: int, task_yaml: dict, debug: bool = False) -> dict:
    """Sends a specific microtask to the Worker LLM to perform changes on Functions or Classes."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent.\n"
        "You are given a YAML microtask containing a 'Task_Requirement' and the current AST representation of a target component (FunctionDef or ClassDef).\n"
        "Your job is to EXECUTE the task by modifying the component's 'body', 'args', or 'return' fields.\n\n"
        "RULES:\n"
        "1. Output ONLY a valid YAML block containing the updated component structure.\n"
        "2. Do NOT include the 'Task_Requirement' block in your response.\n"
        "3. You MUST format the Python code inside the 'body' key as a YAML block scalar using `|`.\n"
        "4. Preserve structural keys like `lineno` and `end_lineno` exactly as provided.\n\n"
        "EXPECTED OUTPUT FORMAT:\n"
        "FunctionDef:\n"
        "  my_function:\n"
        "    lineno: 10\n"
        "    end_lineno: 15\n"
        "    args: [arg1, arg2]\n"
        "    return: str\n"
        "    body: |\n"
        "      result = arg1 + arg2\n"
        "      return str(result)\n"
    )

    user_prompt = yaml.safe_dump(task_yaml, default_flow_style=False, sort_keys=False)

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        clean_yaml = clean_yaml_output(raw_output)
        return yaml.safe_load(clean_yaml)
    except Exception as e:
        print(f"[!] Worker LLM Error: {e}")
        return {}

def execute_module_body_agent(host: str, port: int, task_yaml: dict, debug: bool = False) -> dict:
    """Dedicated micro-agent explicitly for handling modifications to the top-level Module body."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Python Developer Agent specializing in top-level module code.\n"
        "You are given a YAML microtask containing a 'Task_Requirement' and the 'Module' component which contains the top-level 'body' (imports, globals, constants, logic) of the script.\n"
        "Your job is to EXECUTE the task by modifying the 'body' field.\n\n"
        "RULES:\n"
        "1. Output ONLY a valid YAML block containing the updated 'Module' structure.\n"
        "2. Do NOT include the 'Task_Requirement' block in your response.\n"
        "3. You MUST format the Python code inside the 'body' key as a YAML block scalar using `|`.\n"
        "4. Output only valid YAML. No inline strings with escaped linebreaks (`\\n`).\n\n"
        "EXPECTED OUTPUT FORMAT:\n"
        "Module:\n"
        "  body: |\n"
        "    import os\n"
        "    import sys\n"
        "    \n"
        "    GLOBAL_VAR = 'example'\n"
    )

    user_prompt = yaml.safe_dump(task_yaml, default_flow_style=False, sort_keys=False)

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        clean_yaml = clean_yaml_output(raw_output)
        return yaml.safe_load(clean_yaml)
    except Exception as e:
        print(f"[!] Module Body Worker LLM Error: {e}")
        # If debugging, print exactly what failed to parse
        if debug:
            print(f"[DEBUG] Failed Output:\n{raw_output}")
        return {}


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

def extract_microtasks(llm_output: str, raw_ast_data: dict) -> list:
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
                
                # Handling special Module body tasks
                if key == 'Module' and isinstance(val, dict):
                    if val is None:
                        val = {}
                        task_yaml[key] = val
                    if 'Module' in raw_ast_data and 'body' in raw_ast_data['Module']:
                        if 'body' not in task_yaml['Module']:
                            task_yaml['Module']['body'] = ""
                        task_yaml['Module']['body'] = raw_ast_data['Module']['body']
                
                # Functions and Classes
                elif key in ['FunctionDef', 'ClassDef'] and isinstance(val, dict):
                    for comp_name, comp_data in val.items():
                        if comp_data is None:
                            comp_data = {}
                            val[comp_name] = comp_data

                        raw_comp = find_component_in_raw(raw_ast_data, key, comp_name)
                        if raw_comp:
                            for prop in ['args', 'return', 'body', 'lineno', 'end_lineno']:
                                if prop in raw_comp:
                                    comp_data[prop] = raw_comp[prop]
                        
                        if key == 'ClassDef' and 'FunctionDef' in comp_data:
                            for meth_name, meth_data in comp_data['FunctionDef'].items():
                                if meth_data is None:
                                    meth_data = {}
                                    comp_data['FunctionDef'][meth_name] = meth_data

                                raw_method = find_component_in_raw(raw_ast_data, 'FunctionDef', meth_name)
                                if raw_method:
                                    for prop in ['args', 'return', 'body', 'lineno', 'end_lineno']:
                                        if prop in raw_method:
                                            meth_data[prop] = raw_method[prop]
            tasks.append(task_yaml)
        except yaml.YAMLError as e:
            print(f"[!] Failed to parse YAML for a task: {e}")
            
    return tasks

def update_raw_ast_data(raw_ast_data: dict, updated_comp: dict):
    """Merges the updated component from the Worker Agent back into the raw AST data in memory."""
    for key, val in updated_comp.items():
        if key == 'Task_Requirement':
            continue
            
        if key == 'Module' and key in raw_ast_data:
            if 'body' in val:
                raw_ast_data['Module']['body'] = val['body']
                
        elif key in ['FunctionDef', 'ClassDef']:
            for comp_name, comp_data in val.items():
                if key == 'FunctionDef' and 'FunctionDef' in raw_ast_data:
                    if comp_name in raw_ast_data['FunctionDef']:
                        raw_ast_data['FunctionDef'][comp_name].update(comp_data)
                        
                elif key == 'ClassDef' and 'ClassDef' in raw_ast_data:
                    if comp_name in raw_ast_data['ClassDef']:
                        for k, v in comp_data.items():
                            if k != 'FunctionDef':
                                raw_ast_data['ClassDef'][comp_name][k] = v
                        if 'FunctionDef' in comp_data:
                            for m_name, m_data in comp_data['FunctionDef'].items():
                                raw_ast_data['ClassDef'][comp_name]['FunctionDef'][m_name].update(m_data)

# ---------------------------------------------------------
# Code Replacement Engine
# ---------------------------------------------------------
def generate_python_snippet(comp_type: str, comp_name: str, comp_data: dict, is_method: bool = False) -> list:
    """Converts a YAML component dictionary back into Python source lines."""
    lines = []
    indent = "    " if is_method else ""
    
    if comp_type == 'FunctionDef':
        args = ", ".join(comp_data.get('args', []))
        ret = comp_data.get('return', 'None')
        lines.append(f"{indent}def {comp_name}({args}) -> {ret}:")
        
        body = comp_data.get('body', 'pass').strip()
        for line in body.split('\n'):
            lines.append(f"{indent}    {line}")
            
    elif comp_type == 'ClassDef':
        lines.append(f"class {comp_name}:")
        if 'FunctionDef' in comp_data and comp_data['FunctionDef']:
            for meth_name, meth_data in comp_data['FunctionDef'].items():
                lines.extend(generate_python_snippet('FunctionDef', meth_name, meth_data, is_method=True))
                lines.append("")
        else:
            lines.append("    pass")
            
    return lines

def apply_targeted_replacements(original_script_path: str, raw_ast_data: dict, updated_tasks: list):
    """Replaces modified blocks using the lineno and end_lineno directly from the YAML tasks."""
    with open(original_script_path, 'r', encoding='utf-8') as f:
        original_lines = f.read().splitlines()
    
    replacements = []
    
    for task in updated_tasks:
        for key, val in task.items():
            if key in ['FunctionDef', 'ClassDef'] and isinstance(val, dict):
                for comp_name, comp_data in val.items():
                    if 'lineno' in comp_data and 'end_lineno' in comp_data:
                        snippet = generate_python_snippet(key, comp_name, comp_data)
                        replacements.append({
                            "start": comp_data['lineno'],
                            "end": comp_data['end_lineno'],
                            "snippet": snippet
                        })
                        
                    if key == 'ClassDef' and 'FunctionDef' in comp_data:
                        for meth_name, meth_data in comp_data['FunctionDef'].items():
                            if 'lineno' in meth_data and 'end_lineno' in meth_data:
                                snippet = generate_python_snippet('FunctionDef', meth_name, meth_data, is_method=True)
                                replacements.append({
                                    "start": meth_data['lineno'],
                                    "end": meth_data['end_lineno'],
                                    "snippet": snippet
                                })

    unique_replacements = {r['start']: r for r in replacements}
    sorted_replacements = sorted(unique_replacements.values(), key=lambda x: x['start'], reverse=True)

    for rep in sorted_replacements:
        start_idx = rep['start'] - 1  # 0-indexed
        end_idx = rep['end']          # 0-indexed, exclusive slice
        original_lines[start_idx:end_idx] = rep['snippet']

    with open(original_script_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(original_lines) + "\n")

# ---------------------------------------------------------
# CLI & Main Loop
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Microtask Composer & Execution Loop")
    parser.add_argument('--port', type=int, default=8080, help='Port for the local LLM API')
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--archer', type=str, default='archer_filled.yaml', help='Path to the architecture report')
    parser.add_argument('--script', type=str, required=True, help='Path to the original Python script')
    parser.add_argument('--prompt', type=str, required=True, help='User intent/request for updating the codebase')
    parser.add_argument('--debug', action='store_true', help='Print verbose outputs')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.archer) or not os.path.exists(args.script):
        print("[!] Missing architecture report YAML or original Python script file.")
        return

    with open(args.archer, 'r', encoding='utf-8') as f:
        raw_ast_data = yaml.safe_load(f)

    # Filter context to ONLY the Architecture section
    if 'Architecture' not in raw_ast_data:
        print("[!] Cannot find a valid 'Architecture' key in the architecture report.")
        return

    filtered_context = {'Architecture': raw_ast_data['Architecture']}
    architect_context_str = yaml.safe_dump(filtered_context, default_flow_style=False, sort_keys=False, width=float("inf"))

    # 1. Compose Microtasks using only the filtered high-level context
    llm_response = compose_microtasks_with_llm(args.host, args.port, architect_context_str, args.prompt, args.debug)
    
    # Extract tasks and inject the detailed FunctionDef/ClassDef data from the raw in-memory AST
    tasks = extract_microtasks(llm_response, raw_ast_data)
    
    if not tasks:
        print("[!] No valid microtasks generated.")
        return

    print(f"\n[*] Generated {len(tasks)} microtasks. Starting Worker Agent Loop...")
    
    # 2. Execute Microtasks (Agent Loop)
    for i, task in enumerate(tasks, 1):
        print(f"    -> Executing Task {i}/{len(tasks)}...")
        
        # Route to the appropriate micro-agent depending on what component is being targeted
        if 'Module' in task:
            print("       [~] Routing to dedicated Module Body Agent...")
            updated_comp = execute_module_body_agent(args.host, args.port, task, args.debug)
        else:
            updated_comp = execute_worker_agent(args.host, args.port, task, args.debug)
        
        if updated_comp:
            update_raw_ast_data(raw_ast_data, updated_comp)
            
            # Merge worker output into the task tracking object
            task.update(updated_comp)
            
            # Forcefully inject lineno/end_lineno back from original raw_ast_data
            for key, val in task.items():
                if key in ['FunctionDef', 'ClassDef'] and isinstance(val, dict):
                    for comp_name, comp_data in val.items():
                        raw_comp = find_component_in_raw(raw_ast_data, key, comp_name)
                        if raw_comp:
                            if 'lineno' in raw_comp: comp_data['lineno'] = raw_comp['lineno']
                            if 'end_lineno' in raw_comp: comp_data['end_lineno'] = raw_comp['end_lineno']
                        if key == 'ClassDef' and 'FunctionDef' in comp_data:
                            for meth_name, meth_data in comp_data['FunctionDef'].items():
                                raw_method = find_component_in_raw(raw_ast_data, 'FunctionDef', meth_name)
                                if raw_method:
                                    if 'lineno' in raw_method: meth_data['lineno'] = raw_method['lineno']
                                    if 'end_lineno' in raw_method: meth_data['end_lineno'] = raw_method['end_lineno']

            print("       [+] Component updated in memory.")
            
            if args.debug:
                print(f"\n       [DEBUG] Micro Report for Task {i} after execution:")
                print("       " + "-" * 50)
                task_str = yaml.safe_dump(task, default_flow_style=False, sort_keys=False)
                for line in task_str.splitlines():
                    print(f"       {line}")
                print("       " + "-" * 50 + "\n")
            
        else:
            print("       [-] Worker failed to update component.")

    # 3. Replace Blocks in Original Python Script
    # Note: Dedicated replacement logic for 'Module.body' inside original source files remains unmapped
    # and would require full file replacement/AST unparsing; apply_targeted_replacements operates on classes/defs.
    print(f"\n[*] Applying targeted replacements to `{args.script}`...")
    apply_targeted_replacements(args.script, raw_ast_data, tasks)
    
    print(f"[+] Process complete. Code successfully updated in {args.script}.")

if __name__ == "__main__":
    main()
