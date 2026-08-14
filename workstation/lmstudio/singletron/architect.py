#!/usr/bin/env python
import argparse
import os
import requests
import json
import yaml
import glob

# ---------------------------------------------------------
# PyYAML Configuration (Preserve Multiline Strings as | )
# ---------------------------------------------------------
def str_presenter(dumper, data):
    """Ensures multiline strings (like our new body) use the block scalar '|' format."""
    if '\n' in data or len(data.splitlines()) > 1:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)

yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)

def ensure_multiline_bodies(data):
    """Recursively ensure all 'body' keys end with a newline to force PyYAML block formatting."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k == 'body' and isinstance(v, str):
                if not v.endswith('\n'):
                    data[k] = v + '\n'
            else:
                ensure_multiline_bodies(v)
    elif isinstance(data, list):
        for item in data:
            ensure_multiline_bodies(item)

# ---------------------------------------------------------
# LLM Integration: Knowledge Extraction
# ---------------------------------------------------------
def fill_knowledge_with_llm(host: str, port: int, func_name: str, args: list, ret_type: str, source_code: str, debug: bool = False) -> dict:
    """Sends the raw function code to the LLM to deduce architecture and summarize its purpose."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert software architect. You will be provided with a function's name, arguments, return type, and raw source code.\n"
        "Your task is to figure out the knowledge to provide a high-level description.\n\n"
        "RULES:\n"
        "1. Output ONLY a valid JSON object. No conversational text, no markdown formatting outside of the JSON block.\n"
        "2. 'Description': A brief 1-sentence summary of what the function is.\n\n"
        "EXPECTED JSON FORMAT:\n"
        "{\n"
        '  "Description": "..."\n'
        "}"
    )
    
    user_prompt = (
        f"Function: `{func_name}`\n"
        f"Args: {args}\n"
        f"Returns: {ret_type}\n\n"
        f"Raw Code:\n```python\n{source_code}\n```"
    )

    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1024
    }

    if debug:
        print("\n[DEBUG] === OUTGOING LLM PAYLOAD ===")
        print(json.dumps(payload, indent=2))
        print("[DEBUG] ============================\n")

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        if debug:
            print("\n[DEBUG] === RAW LLM RESPONSE ===")
            print(raw_output)
            print("[DEBUG] ========================\n")
            
        if raw_output.startswith("```json"):
            raw_output = raw_output[7:]
        if raw_output.startswith("```"):
            raw_output = raw_output[3:]
        if raw_output.endswith("```"):
            raw_output = raw_output[:-3]
            
        return json.loads(raw_output.strip())
        
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        print(f"[!] Error processing `{func_name}`: {e}")
        return {}

def fill_class_knowledge_with_llm(host: str, port: int, class_name: str, source_code: str, debug: bool = False) -> str:
    """Gets a high-level summary for a class based on its name and raw source code."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    system_prompt = (
        "You are an expert software architect. You will be provided with a class name and its raw source code.\n"
        "Your task is to provide a brief 1-sentence description of the class's overall purpose.\n"
        "RULES: Output ONLY a valid JSON object with a single key 'Description'.\n"
        "{\n  \"Description\": \"...\"\n}"
    )
    
    user_prompt = f"Class: {class_name}\n\nRaw Code:\n```python\n{source_code}\n```"
    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 1024
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        if raw_output.startswith("```json"): raw_output = raw_output[7:]
        if raw_output.startswith("```"): raw_output = raw_output[3:]
        if raw_output.endswith("```"): raw_output = raw_output[:-3]
            
        return json.loads(raw_output.strip()).get("Description", "")
    except Exception as e:
        print(f"[!] Error processing class `{class_name}`: {e}")
        return ""

def fill_module_workflow_with_llm(host: str, port: int, module_name: str, source_code: str, functions_info: str, classes_info: str, debug: bool = False) -> str:
    """Gets a high-level summary of the module's entire architectural big picture."""
    endpoint = f"http://{host}:{port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert software architect analyzing a Python module.\n"
        "You will receive the module's top-level execution code along with a list of its functions and classes (including their arguments, returns, and summaries).\n"
        "Your task is to provide a comprehensive high-level description of the module's overarching workflow. "
        "Show the 'big picture': explain how the components interact, the general data flow, and the main routine executed at the top level.\n"
        "RULES: Output ONLY a valid JSON object with a single key 'Description'. The value should be a detailed, multi-sentence paragraph explaining the workflow.\n"
        "{\n  \"Description\": \"...\"\n}"
    )
    
    user_prompt = (
        f"Module Name: {module_name}\n\n"
        f"Functions (with Signatures & Summaries):\n{functions_info}\n\n"
        f"Classes (with Summaries):\n{classes_info}\n\n"
        f"Top-level Execution Code:\n```python\n{source_code}\n```"
    )
    
    payload = {
        "model": "gemma", 
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 8192
    }

    if debug:
        print("\n[DEBUG] === OUTGOING MODULE LLM PAYLOAD ===")
        print(json.dumps(payload, indent=2))
        print("[DEBUG] ===================================\n")

    try:
        response = requests.post(endpoint, json=payload, timeout=600)
        response.raise_for_status()
        raw_output = response.json()['choices'][0]['message']['content'].strip()
        
        if debug:
            print("\n[DEBUG] === RAW MODULE LLM RESPONSE ===")
            print(raw_output)
            print("[DEBUG] ===============================\n")

        if raw_output.startswith("```json"): raw_output = raw_output[7:]
        if raw_output.startswith("```"): raw_output = raw_output[3:]
        if raw_output.endswith("```"): raw_output = raw_output[:-3]
            
        return json.loads(raw_output.strip()).get("Description", "")
    except Exception as e:
        print(f"[!] Error processing workflow for module `{module_name}`: {e}")
        return ""

# ---------------------------------------------------------
# YAML Traversal and Processing
# ---------------------------------------------------------
def process_function_node(func_name: str, func_data: dict, host: str, port: int, debug: bool) -> str:
    """Processes a single function node and returns the high-level description."""
    print(f"    -> Analyzing Function: {func_name}")
    
    args = func_data.get('args', [])
    ret_type = func_data.get('return', 'None')
    raw_body = func_data.get('body', '')
    
    if not raw_body.strip():
        return ""

    knowledge = fill_knowledge_with_llm(host, port, func_name, args, ret_type, raw_body, debug)
    return knowledge.get('Description', '')

def update_placeholders(ast_data: dict, name: str, description: str, class_name: str = None, is_workflow: bool = False):
    """Writes the high-level description into the architecture placeholders."""
    if 'Architecture' not in ast_data:
        return

    if is_workflow:
        # Update workflow description
        workflow = ast_data['Architecture'].get('workflow', '')
        if isinstance(workflow, str) and "<MODULE_DESCRIPTION>" in workflow:
            ast_data['Architecture']['workflow'] = workflow.replace("<MODULE_DESCRIPTION>", description)
        return

    definition = ast_data['Architecture'].get('definition', {})

    if not class_name:
        # Update standalone function descriptions
        funcs = definition.get('functions', []) or []
        for f_dict in funcs:
            if name in f_dict and isinstance(f_dict[name], str) and "<DESCRIPTION>" in f_dict[name]:
                f_dict[name] = f_dict[name].replace("<DESCRIPTION>", description)
                return

        # Update root-level class descriptions
        classes = definition.get('class', []) or []
        for c_dict in classes:
            if name in c_dict and isinstance(c_dict[name], str) and "<CLASS_DESCRIPTION>" in c_dict[name]:
                c_dict[name] = c_dict[name].replace("<CLASS_DESCRIPTION>", description)
                return

def get_old_description(old_archer: dict, comp_type: str, comp_name: str = None):
    """Retrieves the previous LLM description for a component if it exists."""
    if not old_archer or 'Architecture' not in old_archer:
        return None
    
    definition = old_archer['Architecture'].get('definition', {})
    
    if comp_type == 'FunctionDef':
        for f in definition.get('functions', []):
            if comp_name in f: return f[comp_name]
    elif comp_type == 'ClassDef':
        for c in definition.get('class', []):
            if comp_name in c: return c[comp_name]
    elif comp_type == 'Module':
        return old_archer['Architecture'].get('workflow', None)
        
    return None

def main():
    parser = argparse.ArgumentParser(description="Architect Agent Loop - YAML Knowledge Filler")
    parser.add_argument('--port', type=int, default=8080, help='Port for the local LLM API')
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--input', type=str, required=True, help='Path to the raw AST YAML generated by mkast.py')
    parser.add_argument('--output', type=str, default=None, help='Path to save the consolidated architectural report')
    parser.add_argument('--selective', action='store_true', help='Only update LLM descriptions for components modified in .meta')
    parser.add_argument('--debug', action='store_true', help='Print full LLM payloads and responses')
    
    args = parser.parse_args()
    
    if not args.output:
        input_dir = os.path.dirname(os.path.abspath(args.input))
        args.output = os.path.join(input_dir, 'archer.yaml')

    # BYPASS LOGIC: Skip regeneration if file exists and we are not doing a selective update
    if not args.selective and os.path.exists(args.output):
        print(f"[*] Architectural report '{args.output}' already exists. Bypassing full regeneration.")
        print(f"[*] (Run 'make reset' if you want to rebuild it from scratch).")
        # Touch the file to update its timestamp so the Makefile dependency is satisfied
        os.utime(args.output, None)
        return

    if not os.path.exists(args.input):
        print(f"[!] Input file '{args.input}' not found.")
        return

    print(f"[*] Starting Architect Loop (YAML Knowledge Filler Mode)")
    print(f"[*] Reading AST YAML: {args.input}")
    print("-" * 50)

    try:
        with open(args.input, 'r', encoding='utf-8') as f:
            ast_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"[!] Failed to parse YAML syntax. Details:\n{e}")
        return

    if not ast_data:
        print("[!] Failed to parse YAML or file is empty.")
        return

    # Track components that actually need LLM queries if selective mode is on
    changed_components = set()
    old_archer = {}
    
    if args.selective:
        print("[*] Selective Mode Enabled: Bypassing unchanged components.")
        if os.path.exists(args.output):
            try:
                with open(args.output, 'r', encoding='utf-8') as f:
                    old_archer = yaml.safe_load(f) or {}
            except yaml.YAMLError:
                pass
                
        # Scan meta directory to see what was touched by microtasker
        meta_dir = os.path.dirname(os.path.abspath(args.input))
        for comp_file in glob.glob(os.path.join(meta_dir, '*.*')):
            filename = os.path.basename(comp_file)
            if not filename.endswith('.yaml'):
                changed_components.add(filename)

    # Process Standalone Functions
    if 'FunctionDef' in ast_data and ast_data['FunctionDef']:
        print("[*] Processing Standalone Functions...")
        for func_name, func_data in ast_data['FunctionDef'].items():
            if args.selective and f"FunctionDef.{func_name}" not in changed_components:
                old_desc = get_old_description(old_archer, 'FunctionDef', func_name)
                if old_desc and "<DESCRIPTION>" not in old_desc:
                    print(f"    -> Skipping LLM for {func_name} (Unchanged)")
                    update_placeholders(ast_data, func_name, old_desc)
                    continue
                    
            desc = process_function_node(func_name, func_data, args.host, args.port, args.debug)
            if desc:
                update_placeholders(ast_data, func_name, desc)

    # Process Classes 
    if 'ClassDef' in ast_data and ast_data['ClassDef']:
        print("[*] Processing Classes...")
        for class_name, class_data in ast_data['ClassDef'].items():
            if args.selective and f"ClassDef.{class_name}" not in changed_components:
                old_desc = get_old_description(old_archer, 'ClassDef', class_name)
                if old_desc and "<CLASS_DESCRIPTION>" not in old_desc:
                    print(f"    -> Skipping LLM for {class_name} (Unchanged)")
                    update_placeholders(ast_data, class_name, old_desc)
                    continue

            print(f"    -> Analyzing Class: {class_name}")
            raw_body = class_data.get('body', '')
            class_desc = fill_class_knowledge_with_llm(args.host, args.port, class_name, raw_body, args.debug)
            if class_desc:
                update_placeholders(ast_data, class_name, class_desc)

    # Process Top-Level File Logic (Module Workflow)
    if 'Module' in ast_data and isinstance(ast_data['Module'], dict):
        print("[*] Processing Module Workflow (Big Picture)...")
        module_name = ast_data['Module'].get('name', 'unknown')
        
        skip_module = False
        if args.selective and f"Module.{module_name}" not in changed_components:
            old_desc = get_old_description(old_archer, 'Module')
            if old_desc and "<MODULE_DESCRIPTION>" not in old_desc:
                skip_module = True
                print(f"    -> Skipping LLM for Module Workflow (Unchanged)")
                update_placeholders(ast_data, module_name, old_desc, is_workflow=True)

        if not skip_module:
            raw_body = ast_data['Module'].get('body', '')
            funcs_list = ast_data.get('Architecture', {}).get('definition', {}).get('functions', []) or []
            classes_list = ast_data.get('Architecture', {}).get('definition', {}).get('class', []) or []
            
            func_summaries = []
            for f in funcs_list:
                for k, v in f.items():
                    sig = ""
                    if 'FunctionDef' in ast_data and k in ast_data['FunctionDef']:
                        f_args = ast_data['FunctionDef'][k].get('args', '')
                        f_ret = ast_data['FunctionDef'][k].get('return', 'None')
                        sig = f" (Args: {f_args}) -> {f_ret}"
                    func_summaries.append(f"- {k}{sig}:\n    {v.strip()}")
                    
            class_summaries = []
            for c in classes_list:
                for k, v in c.items():
                    class_summaries.append(f"- {k}:\n    {v.strip()}")
                    
            functions_info = "\n".join(func_summaries) if func_summaries else "None"
            classes_info = "\n".join(class_summaries) if class_summaries else "None"
            
            has_logic = raw_body and raw_body.strip() and not raw_body.strip().startswith("# No top-level")
            has_components = func_summaries or class_summaries
            
            if has_logic or has_components:
                workflow_desc = fill_module_workflow_with_llm(
                    args.host, args.port, module_name, raw_body, functions_info, classes_info, args.debug
                )
                if workflow_desc:
                    update_placeholders(ast_data, module_name, workflow_desc, is_workflow=True)
            else:
                update_placeholders(ast_data, module_name, "Empty module with no logic or definitions.", is_workflow=True)

    # Extract only the Architecture key
    output_data = {'Architecture': ast_data['Architecture']} if 'Architecture' in ast_data else {}

    # Force PyYAML to format all `body` fields as multiline strings by ensuring a trailing newline
    ensure_multiline_bodies(output_data)

    # Save the consolidated YAML (Architecture key only)
    with open(args.output, 'w', encoding='utf-8') as f:
        yaml.safe_dump(output_data, f, default_flow_style=False, sort_keys=False, width=float("inf"))
        
    print("-" * 50)
    print(f"[+] Consolidated architecture report successfully saved to {args.output}")

if __name__ == "__main__":
    main()

