#!/usr/bin/env python
import argparse
import os
import subprocess
import yaml
import requests
import shlex
import base64
import re
import sys
import threading
import queue

def get_input_with_timeout(prompt: str, timeout: int) -> str:
    """Gets user input with a timeout, returning an empty string if it expires."""
    print(prompt, end='', flush=True)
    q = queue.Queue()
    
    def read_input():
        try:
            q.put(sys.stdin.readline().strip())
        except Exception:
            pass

    t = threading.Thread(target=read_input, daemon=True)
    t.start()
    
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        print("\n[*] Timeout reached.")
        return ""

def get_or_create_test_args(script_path: str, meta_dir: str) -> str:
    """Retrieves the cached test arguments or prompts the user after checking --help."""
    test_args_path = os.path.join(meta_dir, "test.args")
    
    if os.path.exists(test_args_path):
        with open(test_args_path, 'r', encoding='utf-8') as f:
            return f.read().strip()

    print(f"[*] No cached 'test.args' found. Checking script usage...")
    help_result = subprocess.run(["python", script_path, "--help"], capture_output=True, text=True)
    
    if help_result.returncode == 0:
        print("\n**Discovered Usage:**")
        print(help_result.stdout.strip())
    else:
        print("\n[!] Script does not support standard --help or threw an error.")

    print(f"\nPlease provide the exact arguments to execute the test (leave blank for none).")
    print(f"Example for '{os.path.basename(script_path)}': --arg value")
    test_args = input("Arguments: ")
    test_args = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', test_args).strip()
    
    os.makedirs(meta_dir, exist_ok=True)
    with open(test_args_path, 'w', encoding='utf-8') as f:
        f.write(test_args)
        
    return test_args


def evaluate_execution_with_llm(host: str, port: str, prompt: str, stdout: str, stderr: str, archer_data: dict, debug: bool = False) -> str:
    """Uses an LLM to evaluate if the execution log satisfies the original user intent."""
    main_port = int(str(port).split(',')[0].strip())
    endpoint = f"http://{host}:{main_port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Quality Assurance Inspector Agent in a TDD loop.\n"
        "Evaluate if the script's execution successfully meets the original user intent AND has no unintended runtime errors.\n\n"
        "RULES:\n"
        "1. If the execution successfully meets the intent and contains no errors, reply STRICTLY with the word: APPROVED\n"
        "2. If it fails or throws an error, compose a Markdown report detailing which components failed and how to fix them."
    )
    
    user_prompt = (
        f"# Original User Intent\n{prompt}\n\n"
        f"---\n# Execution Log\n"
        f"## Standard Output\n```text\n{stdout or 'None'}\n```\n\n"
        f"## Standard Error\n```text\n{stderr or 'None'}\n```\n\n"
        f"---\n# Architecture Report\n```yaml\n{yaml.dump(archer_data)}\n```"
    )

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
        if debug:
            print("\n[DEBUG] === LLM INSPECTOR RESPONSE ===\n" + raw_output + "\n======================================\n")
        return raw_output
    except Exception as e:
        return f"ERROR: Failed to connect to Inspector LLM. {e}"

def main():
    parser = argparse.ArgumentParser(description="TDD Loop Inspector")
    parser.add_argument('--script', type=str, required=True, help='Path to the target Python script')
    parser.add_argument('--prompt', type=str, required=True, help='Original user intent/request')
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--port', type=str, default='8080', help='Port(s) for the local LLM API')
    parser.add_argument('--debug', action='store_true', help='Print verbose outputs')
    args = parser.parse_args()

    script_path = os.path.abspath(args.script)
    meta_dir = os.path.join(os.path.dirname(script_path), f".{os.path.basename(script_path)}")
    archer_file = os.path.join(meta_dir, "archer.yaml")
    
    test_args = get_or_create_test_args(script_path, meta_dir)
    if "!NOTEST!" in test_args:
        print("\n[*] '!NOTEST!' flag detected. Skipping testing loop.")
        return
    # Construct the full execution command dynamically
    parsed_cmd = ["python", script_path]
    if test_args:
        parsed_cmd.extend(shlex.split(test_args))
        
    test_cmd_display = " ".join(parsed_cmd)


    max_iterations = 3
    iteration = 1

    # --- UPDATED LOOP STRUCTURE ---
    while True:
        if iteration > max_iterations:
            print("\n[!] Reached maximum TDD iterations.")
            choice = get_input_with_timeout("Type r to reset for 3 more cycles or enter to ignore: ", 30)
            if choice.lower() == 'r':
                print("\n[*] Resetting to cycle 1...")
                iteration = 1
            else:
                break
        choice = get_input_with_timeout("Type c to cancel TDD loop immediately or enter to continue: ", 15)
        if choice.lower() == 'c':
            print("[*] Exiting loop as requested.")
            break

        print(f"\n\033[1;30;44m [TDD CYCLE {iteration}/{max_iterations}] Executing Test... \033[0m")
        print(f"[*] Running: {test_cmd_display}")
        
        result = subprocess.run(parsed_cmd, capture_output=True, text=True)
        print("\n**Execution Output**")
        if result.stdout: print(result.stdout.strip())
        if result.stderr: print(f"\033[31m{result.stderr.strip()}\033[0m")
        
        archer_data = {}
        if os.path.exists(archer_file):
            with open(archer_file, 'r', encoding='utf-8') as f:
                archer_data = yaml.safe_load(f) or {}

        evaluation_result = evaluate_execution_with_llm(
            args.host, args.port, args.prompt, result.stdout, result.stderr, archer_data, args.debug
        )
        print(evaluation_result) 
        if "APPROVED" in evaluation_result.upper() and not evaluation_result.startswith("ERROR:"):
            print("\n\033[1;37;42m [✓] Inspector LLM Approved: Execution meets user intent! \033[0m")
            break
            
        print("\n[!] Inspector LLM detected issues. Triggering refinement...")
        
        # Encode the LLM evaluation report safely for the shell
        encoded_prompt = base64.b64encode(evaluation_result.encode('utf-8')).decode('utf-8')
        
        # Trigger Make Merge using the Base64 variable
        print("\n[*] Merging changes and updating architecture...")
        subprocess.run([
            "make", "merge", 
            f"SCRIPT={script_path}", 
            f"PROMPT64={encoded_prompt}",
            f"HOST={args.host}",
            f"PORT={args.port}"
        ])        
        iteration += 1

    if iteration > max_iterations:
        print("\n[!] Reached maximum TDD iterations. Manual review required.")

if __name__ == "__main__":
    main()
