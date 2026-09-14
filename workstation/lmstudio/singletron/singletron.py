#!/usr/bin/env python

# ---- GLOBAL ----
import argparse
import os
import subprocess
import sys
import requests
import json
import yaml
import re
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from typing import Optional, Dict, List
import threading
import base64
# ---- GLOBAL.end ----

# ---- DEFINITIONS (Classes & Functions) ----
def safe_print(*args_print, **kwargs):
    """Thread-safe printing to prevent garbled CLI output."""
    with print_lock:
        print(*args_print, **kwargs)

def model_name(host: str, ports: str, endpoint: str) -> Optional[str]:
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

def get_architect_context(script_path: str) -> str:
    """Reads ONLY architecture and feature reports for the high-level architect context."""
    script_dir = os.path.dirname(os.path.abspath(script_path))
    meta_dir = f".{os.path.basename(script_path)}"
    full_meta_dir = os.path.join(script_dir, meta_dir)
    
    context = ""
    # Removed ast.yaml from bulk load
    for doc in ["archer.yaml", "features.yaml"]:
        doc_path = os.path.join(full_meta_dir, doc)
        if os.path.exists(doc_path):
            with open(doc_path, 'r', encoding='utf-8') as f:
                context += f"\n--- {doc} ---\n{f.read()}\n"
    return context if context else "No existing documentation found."

def fetch_ast_component(script_path: str, comp_name: str) -> str:
    """Retrieves a specific component's implementation from the AST yaml."""
    script_dir = os.path.dirname(os.path.abspath(script_path))
    meta_dir = f".{os.path.basename(script_path)}"
    ast_path = os.path.join(script_dir, meta_dir, "ast.yaml")
    
    if not os.path.exists(ast_path):
        return "AST file not found. Ensure it was generated."
        
    try:
        with open(ast_path, 'r', encoding='utf-8') as f:
            ast_data = yaml.safe_load(f) or {}
    except Exception as e:
        return f"Error reading AST: {e}"
        
    if 'FunctionDef' in ast_data and comp_name in ast_data['FunctionDef']:
        return yaml.dump({comp_name: ast_data['FunctionDef'][comp_name]}, sort_keys=False)
    if 'ClassDef' in ast_data and comp_name in ast_data['ClassDef']:
        return yaml.dump({comp_name: ast_data['ClassDef'][comp_name]}, sort_keys=False)
    if comp_name == 'Module' and 'Module' in ast_data:
        return yaml.dump({'Module': ast_data['Module']}, sort_keys=False)
        
    return f"Component '{comp_name}' not found in AST."

def chat_with_architect(user_input: str, script_path: str, history: List[dict], host: str, port: str) -> str:
    """Sends the design discussion to the LLM and handles deep-dive AST requests."""
    main_port = str(port).split(',')[0].strip()
    endpoint = f"http://{host}:{main_port}/v1/chat/completions"
    
    system_prompt = (
        "You are the Head Architect for this codebase. Review the current architecture, "
        "plan new features, or consult the user on direction based on the provided high-level context.\n"
        "If you need to inspect the low-level source code of a specific function, class, or the main 'Module', "
        "output exactly: [FETCH_AST: ComponentName] (e.g., [FETCH_AST: my_function]). "
        "The system will intercept this, fetch the code, and provide it so you can finish your thought.\n\n"
        f"Context Documents:\n{get_architect_context(script_path)}\n"
    )
    
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_input})
    
    max_fetches = 8 # Prevent infinite looping if the LLM gets stuck
    fetches = 0
    
    while fetches < max_fetches:
        payload = {
            "model": "gemma",
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 8192
        }
        
        try:
            response = requests.post(endpoint, json=payload, timeout=600)
            response.raise_for_status()
            reply = response.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            return f"[!] Error communicating with Architect: {e}"
        
        # Check if the LLM is requesting AST data
        match = re.search(r'\[FETCH_AST:\s*([^\]]+)\]', reply)
        if match:
            comp_name = match.group(1).strip()
            print(f"\n\033[38;5;221m[~] Architect is inspecting implementation of: {comp_name}...\033[0m")
            ast_data = fetch_ast_component(script_path, comp_name)
            
            # Feed the fetched data back into the conversation for the next iteration
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user", 
                "content": f"AST output for {comp_name}:\n```yaml\n{ast_data}\n```\nContinue your analysis."
            })
            fetches += 1
        else:
            return reply
            
    return reply + "\n\n[System: Reached maximum AST fetch limit for this turn.]"

def summarize_design_plan(history: List[dict], host: str, port: str) -> str:
    """Consolidates the design chat history into an actionable implementation plan."""
    main_port = str(port).split(',')[0].strip()
    endpoint = f"http://{host}:{main_port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert software architect. Your task is to review the following design discussion "
        "and consolidate it into a clear, actionable implementation plan focused STRICTLY on Python code changes.\n"
        "CRITICAL RULE: Do NOT include any tasks related to updating documentation, architecture reports, "
        "or metadata files (e.g., archer.yaml, ast.yaml, features.yaml). The pipeline handles these automatically. "
        "Only output instructions for modifying the actual source code logic. Detail the exact logic changes, "
        "new features, and architectural adjustments agreed upon. Output ONLY the plan."
    )
    
    chat_log = ""
    for msg in history:
        chat_log += f"{msg['role'].upper()}:\n{msg['content']}\n\n"
        
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": chat_log}
    ]
    
    payload = {
        "model": "gemma",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096
    }
    
    try:
        response = requests.post(endpoint, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"[!] Error summarizing plan: {e}"

def main():
    parser = argparse.ArgumentParser(description='Vibe Coding Interactive Shell')
    parser.add_argument('-f', '--script', required=True, help='Path to Python script target')
    parser.add_argument('--host', default=os.getenv('HOST', 'localhost'), help='LLM Server Host')
    parser.add_argument('--port', default=os.getenv('PORT', '8080'), help='LLM Server Port(s) (comma-separated)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    
    base_filename = os.path.basename(args.script)
    MODEL_NAME = model_name(args.host, args.port, endpoint='/models')
    HISTORY_FILE = ".singletron.py.history"
    
    if MODEL_NAME:
        safe_print(f'✅ Ready: {MODEL_NAME}')
        
    promptia_session = PromptSession(
        history=FileHistory(os.path.join("./", HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(['==ds', '==dd', '==de'], ignore_case=True)
    )
    
    promptia_style = Style.from_dict({
        'llm': 'bg:#408175 fg:#89D7B7 bold',
        'design': 'bg:#B9D175 fg:#000000 bold', 
        'prompt': 'bg:#000000 fg:#89D7B7',
        'ws': 'bg:#89D7B7 fg:#89D7B7'
    })
    
    print(f'==========================================')
    print(f' Singletron Interactive Shell')
    print(f' Target: {args.script}')
    print(f' Server: {args.host}:{args.port}')
    print(f'==========================================')
    print('Commands: ==ds (Design Mode), ==dd (Design Done & Apply), ==de (Design Exit & Discard)')
    print('Press [Meta+Enter] or [Esc] then [Enter] to submit multiline prompts.')
    print("Type 'exit' or press Ctrl+C to quit.\n")
    
    design_mode = False
    design_history = []
    
    while True:
        try:
            mode_class = 'class:design' if design_mode else 'class:llm'
            mode_text = ' DESIGN ' if design_mode else ' SINGLETRON '
            
            user_input = promptia_session.prompt(
                [(mode_class, mode_text), ('class:prompt', f' {base_filename} '), ('class:ws', ' ')], 
                multiline=True, 
                style=promptia_style
            ).strip()
            
            if not user_input:
                continue
            if user_input.lower() in ('exit', 'quit'):
                print('Exiting vibe shell.')
                break
                
            # Mode Toggles
            if user_input == '==ds':
                print("\n\033[1;35m[+] Preparing Design Mode. Extracting AST and Architecture...\033[0m")
                
                # Execute the 'architect' target which ONLY runs mkast.py and architect.py
                cmd = ['make', 'architect', f'SCRIPT={args.script}', f'HOST={args.host}', f'PORT={args.port}']
                if args.debug:
                    cmd.append('DEBUG=true')
                subprocess.run(cmd)

                design_mode = True
                print("\n\033[1;35m[+] Entered Design Mode. Chatting with Head Architect.\033[0m\n")
                continue

            elif user_input == '==de':
                design_mode = False
                design_history.clear()
                print("\n\033[1;31m[-] Left Design Mode. Chat discarded.\033[0m\n")
                continue
                
            elif user_input == '==dd':
                if not design_mode:
                    print("\n[!] Not in design mode.\n")
                    continue
                    
                print("\n\033[1;33m[~] Consolidating design discussion into an actionable plan...\033[0m\n")
                plan = summarize_design_plan(design_history, args.host, args.port)
                
                print(f"\n\033[38;5;4m--- Proposed Implementation Plan ---\033[0m\n\033[38;5;6m{plan}\033[0m\n\033[38;5;4m------------------------------------\033[0m\n")
                
                approval = input("\033[1mProceed with this plan? (y/n):\033[0m ").strip().lower()
                
                if approval == 'y':
                    print("\n\033[1;32m[+] Passing implementation plan to microtasker...\033[0m\n")
                    prompt64 = base64.b64encode(plan.encode('utf-8')).decode('utf-8')
                    design_mode = False
                    design_history.clear()
                    
                    cmd = ['make', 'run', f'SCRIPT={args.script}', f'PROMPT64={prompt64}', f'HOST={args.host}', f'PORT={args.port}']
                    if args.debug:
                        cmd.append('DEBUG=true')
                    subprocess.run(cmd)
                    print()
                else:
                    print("\n\033[1;31m[-] Plan discarded. Returning to design mode.\033[0m\n")
                continue
            
            # Sub-loop logic based on active mode
            if design_mode:
                reply = chat_with_architect(user_input, args.script, design_history, args.host, args.port)
                print(f"\n\033[38;5;213mArchitect:\033[0m\n{reply}\n")
                design_history.append({"role": "user", "content": user_input})
                design_history.append({"role": "assistant", "content": reply})
            else:
                prompt64 = base64.b64encode(user_input.encode('utf-8')).decode('utf-8')
                cmd = ['make', 'run', f'SCRIPT={args.script}', f'PROMPT64={prompt64}', f'HOST={args.host}', f'PORT={args.port}']
                if args.debug:
                    cmd.append('DEBUG=true')

                subprocess.run(cmd)
                print()
                
        except (KeyboardInterrupt, EOFError):
            print('\nExiting vibe shell.')
            break

# ---- DEFINITIONS.end ----

# ---- MAIN ---
print_lock = threading.Lock()
if __name__ == '__main__':
    main()
# ---- MAIN.end ----
