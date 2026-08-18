#!/usr/bin/env python
import argparse
import os
import subprocess
import sys
import requests
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style
from typing import Optional, Dict, List
import threading

print_lock = threading.Lock() 

def safe_print(*args_print, **kwargs):
    """Thread-safe printing to prevent garbled CLI output."""
    with print_lock:
        print(*args_print, **kwargs)

def model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if 'models' in data and data['models']:
            return data['models'][0].get('name')
    except Exception as e:
        safe_print(f"\n❌ ERROR connecting to LLM: {e}")
    return None

def main():
    parser = argparse.ArgumentParser(description="Vibe Coding Interactive Shell")
    parser.add_argument("-f", "--script", required=True, help="Path to Python script target")
    parser.add_argument("--host", default=os.getenv("HOST", "localhost"), help="LLM Server Host")
    parser.add_argument("--port", default=os.getenv("PORT", "8080"), help="LLM Server Port")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    MODEL_NAME = model_name(args.host, args.port, endpoint="/models")
    if MODEL_NAME:
      safe_print(f"✅ Ready: {MODEL_NAME}")

    # Configure prompt_toolkit session and custom styling
    promptia_session = PromptSession()
    promptia_style = Style.from_dict({
        'llm': 'bg:#c4c408 fg:#000000 bold',
        'prompt': 'bg:#000000 fg:#c4c408',
        'ws': 'bg:#c4c408 fg:#c4c408'
    })

    print(f"==========================================")
    print(f" Singletron Interactive Shell")
    print(f" Target: {args.script}")
    print(f" Server: {args.host}:{args.port}")
    print(f"==========================================")
    print("Press [Meta+Enter] or [Esc] then [Enter] to submit multiline prompts.")
    print("Type 'exit' or press Ctrl+C to quit.\n")

    while True:
        try:
            user_input = promptia_session.prompt(
                [('class:llm', ' SINGLETRON '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                multiline=True,
                style=promptia_style
            ).strip()

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                print("Exiting vibe shell.")
                break

            # Build Makefile execution command
            cmd = [
                "make", "run",
                f"SCRIPT={args.script}",
                f"PROMPT={user_input}",
                f"HOST={args.host}",
                f"PORT={args.port}"
            ]
            if args.debug:
                cmd.append("DEBUG=true")

            subprocess.run(cmd)
            print()

        except (KeyboardInterrupt, EOFError):
            print("\nExiting vibe shell.")
            break

if __name__ == "__main__":
    main()
