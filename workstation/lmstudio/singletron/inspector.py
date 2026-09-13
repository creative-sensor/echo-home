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

def get_input_with_timeout(prompt: str, timeout: int) -> str:
    print(prompt, end='', flush=True)
    user_input = [""]
    def read_input():
        user_input[0] = sys.stdin.readline().strip("\n")
    # Run the input prompt in a background thread
    thread = threading.Thread(target=read_input)
    thread.daemon = True
    thread.start()
    
    # Wait for the thread to finish or timeout
    thread.join(timeout)
    if thread.is_alive():
        print() # Move to the next line on timeout
        return ""
    return user_input[0]

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


def evaluate_execution_with_llm(host: str, port: str, prompt: str, test_cmd: str, stdout: str, stderr: str, features_data: dict, debug: bool = False) -> str:
    """Uses an LLM to evaluate if the execution log satisfies the intent AND preserves existing features."""
    main_port = int(str(port).split(',')[0].strip())
    endpoint = f"http://{host}:{main_port}/v1/chat/completions"
    
    system_prompt = (
        "You are an expert Quality Assurance Inspector Agent in a TDD loop.\n"
        "Evaluate if the script's execution successfully meets the original user intent AND has no unintended runtime errors.\n\n"
        "RULES:\n"
        "1. Check the 'Features List'. You MUST verify that the current execution output does not break, contradict, or regress any existing features listed there.\n"
        "2. If the execution meets the intent, contains no errors, AND preserves all existing features, reply STRICTLY with the word: APPROVED\n"
        "3. If it fails, throws an error, or breaks an existing feature, compose a Markdown report detailing which components failed/regressed and how to fix them."
    )
    
    user_prompt = (
        f"# Original User Intent\n{prompt}\n\n"
        f"---\n# Test Run\n```bash\n{test_cmd}\n```\n\n"
        f"---\n# Execution Log\n"
        f"## Standard Output\n```text\n{stdout or 'None'}\n```\n\n"
        f"## Standard Error\n```text\n{stderr or 'None'}\n```\n\n"
        f"---\n# Features List\n```yaml\n{yaml.dump(features_data)}\n```"
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

def generate_feature_description(host: str, port: str, prompt: str, stdout: str, debug: bool = False) -> str:
    """Uses an LLM to write a short description of the feature based on intent and successful output."""
    main_port = int(str(port).split(',')[0].strip())
    endpoint = f"http://{host}:{main_port}/v1/chat/completions"
    
    system_prompt = (
        "You are a technical writer documenting software features. "
        "Based on the user intent and the successful test output, write a single, concise sentence "
        "describing the expected output or behavior of this specific feature. Do not include raw logs."
    )
    
    user_prompt = f"User Intent: {prompt}\n\nStandard Output: {stdout}"
    
    payload = {
        "model": "gemma",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 4096
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception:
        return f"Feature implementation of: {prompt}"


def main():
    parser = argparse.ArgumentParser(description="TDD Loop Inspector")
    parser.add_argument('--script', type=str, required=True, help='Path to the target Python script')
    
    # Make prompt optional and add prompt64
    parser.add_argument('--prompt', type=str, help='Original user intent/request')
    parser.add_argument('--prompt64', type=str, help='Base64 encoded user intent')
    
    parser.add_argument('--host', type=str, default='localhost', help='Host for the local LLM API')
    parser.add_argument('--port', type=str, default='8080', help='Port(s) for the local LLM API')
    parser.add_argument('--debug', action='store_true', help='Print verbose outputs')
    args = parser.parse_args()

    # Ensure at least one prompt is provided
    if not args.prompt and not args.prompt64:
        parser.error("Either --prompt or --prompt64 must be provided.")

    # Decode base64 prompt if provided, otherwise use standard prompt
    if args.prompt64:
        try:
            user_prompt = base64.b64decode(args.prompt64).decode('utf-8')
        except Exception as e:
            print(f"[!] Error decoding --prompt64: {e}")
            sys.exit(1)
    else:
        user_prompt = args.prompt


    script_path = os.path.abspath(args.script)
    meta_dir = os.path.join(os.path.dirname(script_path), f".{os.path.basename(script_path)}")
    archer_file = os.path.join(meta_dir, "archer.yaml")
    features_file = os.path.join(meta_dir, "features.yaml")
    test_file = os.path.join(meta_dir, "test.args")
    
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
        choice = get_input_with_timeout("[🌈] c[stop TDD]  y[update args] : ", 15)
        if choice.lower() == 'c':
            print("[*] Exiting loop as requested.")
            break
        
        if choice.lower() == 'y':
            print(f"Please provide the exact arguments to execute the test (leave blank for none).")
            new_args = input("Arguments: ")
            new_args = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', new_args).strip()
            with open(os.path.join(meta_dir, "test.args"), 'w', encoding='utf-8') as f:
                f.write(new_args)
            test_args = new_args

        test_args = get_or_create_test_args(script_path, meta_dir)
        if "!NOTEST!" in test_args:
            print("\n[*] '!NOTEST!' flag detected. Skipping testing loop.")
            return

        parsed_cmd = ["python", script_path]
        if test_args:
            parsed_cmd.extend(shlex.split(test_args))
            
        test_cmd_display = " ".join(parsed_cmd)
        print(f"\n\033[1;30;44m [TDD CYCLE {iteration}/{max_iterations}] Executing Test... \033[0m")
        print(f"[*] Running: {test_cmd_display}")
        
        result = subprocess.run(parsed_cmd, capture_output=True, text=True)
        print("\n**Execution Output**")
        if result.stdout: print(result.stdout.strip())
        if result.stderr: print(f"\033[31m{result.stderr.strip()}\033[0m")
        
        # Load from features.yaml instead of archer.yaml
        features_data = {}
        if os.path.exists(features_file):
            with open(features_file, 'r', encoding='utf-8') as f:
                features_data = yaml.safe_load(f) or {}

        # Pass test_cmd_display to the LLM
        evaluation_result = evaluate_execution_with_llm(
            args.host, args.port, user_prompt, test_cmd_display, result.stdout, result.stderr, features_data, args.debug
        )
        print(f"\n\033[1;30;44m{evaluation_result}\033[0m") 

        if "APPROVED" in evaluation_result.upper() and not evaluation_result.startswith("ERROR:"):
            print("\n\033[1;37;42m [✓] Inspector LLM Approved: Execution meets user intent & preserves existing features! \033[0m")
            
            # 1. Commit the script to git
            script_name = os.path.basename(script_path)
            commit_msg = f"{script_name}: {user_prompt}"
            subprocess.run(
              ["git", "add",
              script_path,
              archer_file,
              features_file,
              test_file]
            )
            subprocess.run(["git", "commit", "-m", commit_msg])
            
            # 2. Extract feature ID (first 7 digits of commit hash)
            git_hash_proc = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True)
            feature_id = git_hash_proc.stdout.strip()
            
            # 3. Generate summary based on BOTH user intent and actual test output
            expected_output = generate_feature_description(
                args.host, args.port, user_prompt, result.stdout, args.debug
            )
            
            # 4. Save features inline to features.yaml
            if 'features' not in features_data or not features_data['features']:
                features_data['features'] = []
                
            features_data['features'].append({feature_id: expected_output})
            
            with open(features_file, 'w', encoding='utf-8') as f:
                yaml.dump(features_data, f, default_flow_style=False, sort_keys=False, width=float("inf"))
            
            # 5. Backup the test arguments file for future regression tests
            test_args_path = os.path.join(meta_dir, "test.args")
            if os.path.exists(test_args_path):
                import shutil
                shutil.copy(test_args_path, f"{test_args_path}.{feature_id}")
                print(f"[*] Backed up test arguments to test.args.{feature_id}")
                
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
