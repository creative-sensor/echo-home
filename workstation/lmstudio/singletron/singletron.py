#!/usr/bin/env python

# ---- GLOBAL ----
import argparse
import os
import subprocess
import sys
import requests
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
        completer=WordCompleter(['===m'], ignore_case=True)
    )
    promptia_style = Style.from_dict({'llm': 'bg:#408175 fg:#89D7B7 bold', 'prompt': 'bg:#000000 fg:#89D7B7', 'ws': 'bg:#89D7B7 fg:#89D7B7'})
    print(f'==========================================')
    print(f' Singletron Interactive Shell')
    print(f' Target: {args.script}')
    print(f' Server: {args.host}:{args.port}')
    print(f'==========================================')
    print('Press [Meta+Enter] or [Esc] then [Enter] to submit multiline prompts.')
    print("Type 'exit' or press Ctrl+C to quit.\n")
    while True:
        try:
            user_input = promptia_session.prompt([('class:llm', ' SINGLETRON '), ('class:prompt', f' {base_filename} '), ('class:ws', ' ')], multiline=True, style=promptia_style).strip()
            prompt64 = base64.b64encode(user_input.encode('utf-8')).decode('utf-8')
            if not user_input:
                continue
            if user_input.lower() in ('exit', 'quit'):
                print('Exiting vibe shell.')
                break
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
