#!/usr/bin/env python
import os
import json
import subprocess
import re
import argparse
import textwrap
import requests
import uuid
import yaml
import platform
import tempfile
from typing import Optional, Dict, List, Callable, Tuple

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

try:
    import chromadb
except ImportError:
    print("❌ ERROR: Missing dependencies. Run: pip install chromadb pyyaml")
    exit(1)

# ==========================================
# 0. GIT BASH & WINDOWS PATH RESOLVER
# ==========================================
def resolve_path(path_str: str) -> str:
    """
    Safely expands '~' and resolves Git Bash (/c/Users/...) vs Windows (C:/Users/...) 
    path discrepancies, ensuring consistent forward-slashes for bash execution.
    """
    if not path_str:
        return path_str
        
    # 1. Expand '~' respecting Git Bash $HOME over Windows $USERPROFILE if present
    if path_str.startswith("~"):
        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        path_str = os.path.join(home_dir, path_str[1:].lstrip("/\\"))
        
    # 2. Convert Git Bash style drive letters (e.g., /c/Users -> C:/Users)
    if platform.system() == "Windows" or os.environ.get("MSYSTEM"):
        match = re.match(r"^/([a-zA-Z])/(.*)", path_str)
        if match:
            drive, rest = match.groups()
            path_str = f"{drive.upper()}:/{rest}"
            
    # 3. Normalize all slashes to forward slashes for universal Bash compatibility
    return os.path.normpath(path_str).replace("\\", "/")

def to_bash_path(path_str: str) -> str:
    r"""
    Converts a Windows path (e.g., C:/Users/... or C:\Users\...) into a POSIX Git Bash path
    (e.g., /c/Users/...) so MSYS binaries like bash, cat, and sh can locate the file without error.
    """
    if not path_str:
        return path_str
    path_str = path_str.replace("\\", "/")
    match = re.match(r"^([a-zA-Z]):/(.*)", path_str)
    if match:
        drive, rest = match.groups()
        return f"/{drive.lower()}/{rest}"
    return path_str

def parse_llm_json(raw_text: str) -> dict:
    clean_text = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw_text, flags=re.DOTALL)
    clean_text = re.sub(r"```json", "", clean_text)
    clean_text = re.sub(r"```", "", clean_text).strip()
    
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        return {"action_type": "declare_result", "status": "FAILED", "reason": "LLM output invalid JSON."}

# ==========================================
# 1. CLI ARGUMENTS & API SETUP
# ==========================================
parser = argparse.ArgumentParser(description="Memphia: Interactive LLM Agent with Uvian Memory & ChromaDB.")
parser.add_argument('--port', type=int, default=8080, help='Port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='Hostname of the local LLM server')
parser.add_argument('--chunk-size', type=int, default=8192, help='Max characters before Map-Reduce is triggered')
parser.add_argument('--uvian-dir', type=str, default='~/echo-home/memory/uvian', help='Root directory for uvian storage')
parser.add_argument('--summary', type=str, default=None, help='YAML file containing uvian summaries (Default: <uvian-dir>/summer.yaml)')
args = parser.parse_args()

# Sanitize base directories using the path resolver
uvian_dir_bash = args.uvian_dir
args.uvian_dir = resolve_path(args.uvian_dir)

if args.summary is None:
    args.summary = resolve_path(os.path.join(args.uvian_dir, "summer.yaml"))
else:
    args.summary = resolve_path(args.summary)

client = OpenAI(
    base_url=f"http://{args.host}:{args.port}/v1", 
    api_key="localm" 
)

def get_model_name(host: str, port: int, endpoint: str) -> Optional[str]:
    url = f"http://{host}:{port}{endpoint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status() 
        data = response.json()
        if 'models' in data and data['models']:
            first_model = data['models'][0]
            if 'name' in first_model:
                model_name = first_model['name']
                print(f"✅ LLM Ready: {model_name}")
                return model_name
    except Exception as e:
        print(f"\n❌ ERROR connecting to LLM: {e}")
    return None

MODEL_NAME = get_model_name(args.host, args.port, endpoint="/models")

# ==========================================
# 2. UVIAN MEMORY MANAGER (TEXT SEARCH & LLM SELECTION)
# ==========================================
class UvianMemoryManager:
    def __init__(self, host: str = 'localhost', port: int = 8080, model_name: str = 'default_model', uvian_dir: str = './'):
        self.model_name = model_name
        self.uvian_dir = resolve_path(uvian_dir)
        
        # Sanitize model name to prevent illegal directory characters (/ \ : etc.)
        safe_model_name = re.sub(r'[\/\\:]', '_', str(model_name))
        db_path = resolve_path(f"./memphia/vector/{safe_model_name}")
        
        print(f"🧠 Initializing Uvian Memory Base at: {db_path}")
        self.chroma_client = chromadb.PersistentClient(path=db_path)
        self.collection = self.chroma_client.get_or_create_collection(
            name="uvian",
            metadata={"hnsw:space": "cosine"}
        )
        
        self.openai_client = OpenAI(base_url=f"http://{host}:{port}/v1", api_key="localm")

    def sync_yaml_to_chroma(self, yaml_file: str):
        yaml_file = resolve_path(yaml_file)
        if not os.path.exists(yaml_file):
            print(f"⚠️ Warning: Summary file '{yaml_file}' not found. Skipping sync.")
            return
    
        try:
            with open(yaml_file, 'r', encoding='utf-8') as yf:
                raw_lines = yf.readlines()
            
            existing_ids = set(self.collection.get()['ids'])
            
            count = 0
            skipped = 0
            fallbacks = 0
            for line in raw_lines:
                if ":" not in line:
                    continue
                uid_part, json_block = line.split(":", 1)
                uid = uid_part.strip()
                try:
                    models_dict = json.loads(json_block.strip())
                    doc_id = f"{uid}"
                    
                    if doc_id in existing_ids:
                        skipped += 1
                        continue
                        
                    summary = None
                    if self.model_name in models_dict:
                        summary = models_dict[self.model_name]
                    else:
                        dir1 = uid[0:1]
                        dir2 = uid[1:2]
                        dir3 = uid[2:4]
                        file_path = resolve_path(os.path.join(self.uvian_dir, dir1, dir2, dir3, uid))
                        if os.path.exists(file_path):
                            try:
                                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                    summary = f.read().strip()
                                if summary:
                                    fallbacks += 1
                                    print(f"📄 [FALLBACK] Loaded raw content from file for UUID: {uid}")
                            except Exception as fe:
                                print(f"⚠️ [FALLBACK ERROR] Could not read file {file_path}: {fe}")
                        else:
                            print(f"⚠️ [SKIP] No summary for model '{self.model_name}' and file missing at {file_path}")
                            
                    if summary:
                        self.collection.add( 
                            ids=[doc_id],
                            documents=[summary],
                            metadatas=[{"uuid": uid, "model": self.model_name}]
                        )
                        count += 1
                except json.JSONDecodeError:
                    continue
                    
            print(f"🔄 Synced {count} new summaries into Memory Base (Skipped {skipped} existing, {fallbacks} raw file fallbacks).")
        except Exception as e:
            print(f"❌ Error syncing YAML to Memory Base: {e}")

    def retrieve_most_relevant_uuid(self, user_intent: str) -> Optional[Tuple[str, str]]:
        if self.collection.count() == 0:
            return None

        # --- STEP 1: KEYWORD TEXT MATCHING (ANY WORD MATCH) ---
        words = user_intent.split()
        if not words:
            return None

        # Compile word-boundary regexes for each individual word
        patterns = []
        for w in words:
            try:
                patterns.append(re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE))
            except Exception:
                pass

        all_memories = self.collection.get()
        ids = all_memories.get('ids', [])
        documents = all_memories.get('documents', [])

        yaml_lines = []
        candidate_map = {}

        # Search for any single word match across documents
        for doc_id, doc_text in zip(ids, documents):
            matching_lines = [
                line.strip() for line in doc_text.splitlines() 
                if any(p.search(line) for p in patterns)
            ]
            if matching_lines:
                candidate_map[doc_id] = doc_text
                yaml_lines.append(f"\033[48;5;54m\033[38;5;177m{doc_id}\033[0m: |")
                for line in matching_lines:
                    yaml_lines.append(f"    {line}")

        if not candidate_map or not yaml_lines:
            print("🔍 Text matching found no candidates in memory.")
            return None

        yaml_output = "\n".join(yaml_lines)
        print("\n📄 [TEXT MATCH CANDIDATES (YAML)]\n```yaml")
        print(yaml_output)
        print("```")

        # --- STEP 2: SEPARATE LLM SELECTION LOOP ---
        select_system_prompt = (
            "You are a UUID selection assistant.\n"
            "Analyze the user's intent and the candidate memory UUID entries provided in YAML format.\n"
            "Select the single UUID that is most relevant to fulfilling the user's intent.\n"
            "If none of the candidate UUIDs are relevant, set selected_uuid to null.\n"
            "Respond strictly in valid JSON format using the following schema:\n"
            '{\n  "selected_uuid": "<uuid or null>",\n  "reason": "<short explanation>"\n}'
        )

        select_user_prompt = (
            f"User Intent: {user_intent}\n\n"
            f"Candidate Memory UUIDs (YAML):\n```yaml\n{yaml_output}\n```"
        )

        try:
            print("🤖 Asking LLM to select the most relevant UUID from candidate matches...")
            response = self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": select_system_prompt},
                    {"role": "user", "content": select_user_prompt}
                ],
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            
            raw_res = response.choices[0].message.content
            parsed = parse_llm_json(raw_res)
            selected_uuid = parsed.get("selected_uuid")
            reason = parsed.get("reason", "")

            if selected_uuid and selected_uuid in candidate_map:
                print(f"🎯 LLM Selected UUID: {selected_uuid} (Reason: {reason})")
                return selected_uuid, candidate_map[selected_uuid]
            else:
                print(f"ℹ️ LLM evaluated candidates and found no relevant UUID match. (Reason: {reason})")
                return None

        except Exception as e:
            print(f"❌ Error during LLM UUID selection: {e}")
            return None

# ==========================================
# 3. UVIAN TOOL & DATA LOADER
# ==========================================
def handle_uvian_entry(uuid_str: str, uvian_dir: str = "./") -> Dict[str, str]:
    if len(uuid_str) < 4:
        return {"type": "error", "content": "UUID string is too short."}
        
    dir1 = uuid_str[0:1]
    dir2 = uuid_str[1:2]
    dir3 = uuid_str[2:4]
    
    file_path = resolve_path(os.path.join(uvian_dir_bash, dir1, dir2, dir3, uuid_str))
    
    if not os.path.exists(file_path):
        return {"type": "error", "content": f"Uvian target file not found at {file_path}"}
        
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
    except Exception as e:
        return {"type": "error", "content": f"Failed to inspect header: {str(e)}"}
        
    is_shebang = first_line.startswith("#!")
    
    if is_shebang:
        if "python" in first_line.lower():
            engine = "python3" if subprocess.call(["command", "-v", "python3"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0 else "python"
        elif "node" in first_line.lower():
            engine = "node"
        else:
            engine = "bash"
            
        if engine == "bash" and (platform.system() == "Windows" or os.environ.get('OS') == 'Windows_NT'):
            git_bash_path = r"C:\Program Files\Git\bin\bash.exe"
            if os.path.exists(git_bash_path):
                engine = git_bash_path
            else:
                print("⚠️ [WARNING] Standard Git Bash path not found. Falling back to system PATH.")
    else:
        engine = "cat"

    is_bash_engine = "bash" in str(engine).lower() or engine in ["sh", "cat"]
    exec_path = to_bash_path(file_path) if is_bash_engine else file_path

    print(f"\n⚡ [UVIAN TOOL CALL] Executing {uuid_str} via '{engine}' engine (Zero-View)...")
    try:
        proc = subprocess.run([engine, exec_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        
        print(f"\033[32m[UVIAN EXIT CODE]\033[0m {proc.returncode}")
        if proc.stdout.strip():
            print(f"\033[32m[UVIAN STDOUT]\033[0m\n\033[38;5;108m{proc.stdout.strip()}\033[0m")
        if proc.stderr.strip():
            print(f"\033[31m[UVIAN STDERR]\033[0m\n{proc.stderr.strip()}")
            
        return {
            "type": "tool_execution",
            "uuid": uuid_str,
            "engine": engine,
            "exit_code": str(proc.returncode),
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip()
        }
    except Exception as e:
        return {"type": "error", "content": f"Execution failed: {str(e)}"}

# ==========================================
# 4. LLM & MAP-REDUCE CAPABILITY
# ==========================================
def standard_llm_call(system_prompt: str, user_prompt: str) -> str:
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.0
    )
    return response.choices[0].message.content

class VRAMOptimizedMapReduceAgent:
    def __init__(self, llm_call: Callable[[str, str], str], max_chars_per_chunk: int):
        self.llm_call = llm_call
        self.max_chars_per_chunk = max_chars_per_chunk

    def _chunk_text(self, text: str) -> List[str]:
        return textwrap.wrap(text, width=self.max_chars_per_chunk, break_long_words=False)

    def run(self, task_description: str, input_data: str) -> str:
        if len(input_data) <= self.max_chars_per_chunk:
            return input_data 

        print(f"\n\033[35m[MAP-REDUCE]\033[0m Output too large ({len(input_data)} chars). Summarizing for LLM context window...")
        chunks = self._chunk_text(input_data)
        chunk_results = []
        
        map_system_prompt = (
            "You are a command-line data extraction agent. "
            "Extract and summarize ONLY information relevant to the user's task. "
            "If the chunk contains no relevant information, reply exactly with 'NO RELEVANT INFO'."
        )
        
        for i, chunk in enumerate(chunks):
            print(f"  -> Processing chunk {i + 1}/{len(chunks)}...")
            user_prompt = f"User Intent: {task_description}\n\nChunk Output:\n{chunk}"
            result = self.llm_call(map_system_prompt, user_prompt)
            if "NO RELEVANT INFO" not in result.upper():
                chunk_results.append(f"--- Info from chunk {i+1} ---\n{result}")

        print("  -> Consolidating final summary...")
        consolidated_context = "\n\n".join(chunk_results)
        if not consolidated_context.strip():
            return "Map-Reduce resulted in no relevant findings."

        reduce_system_prompt = "You are a synthesis agent. Consolidate extracted terminal outputs into a final summary."
        return self.llm_call(reduce_system_prompt, f"Task: {task_description}\n\nFindings:\n{consolidated_context}")

# ==========================================
# 5. SHELL AGENT CORE & AUTOPILOT
# ==========================================
CURRENT_CWD = resolve_path(os.getcwd())
LAST_UUID = None

SYSTEM_PROMPT = """<|think|>
You are Memphia, an interactive shell operator with autonomous memory tool execution.
Your goal is to fulfill the User Intent, verify it worked using stdout/stderr proof, and report the final status.

RULES:
1. Operate in a loop: issue a shell command or declare the task finished.
2. Do not declare SUCCESS without explicit proof from stdout/stderr or provided Uvian facts.
3. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_command" or "declare_result",
  "command": "<shell command> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for result> or null"
}
"""

def is_autopilot_enabled() -> bool:
    autopilot_path = resolve_path("~/echo-home/autopilot.yaml")
    if os.path.exists(autopilot_path):
        try:
            with open(autopilot_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            allow_val = config.get("memphia", {}).get("allow", False)
            if allow_val is True or str(allow_val).strip().lower() in ['yes', 'true', '1', 'y']:
                return True
        except Exception as e:
            print(f"\n⚠️ [AUTOPILOT WARNING] Failed to read {autopilot_path}: {e}")
    return False

def get_os_env_context() -> str:
    global CURRENT_CWD
    sys_name = platform.system()
    release = platform.release()
    arch = platform.machine()
    cwd = to_bash_path(CURRENT_CWD)
    user = os.environ.get('USER', os.environ.get('USERNAME', 'unknown'))
    shell = os.environ.get('SHELL', 'default/bash')
    
    return (
        f"\n\n[OS ENVIRONMENT CONTEXT]\n"
        f"- OS/Kernel: {sys_name} {release} ({arch})\n"
        f"- Current User: {user}\n"
        f"- Working Directory: {cwd}\n"
        f"- Default Shell: {shell}"
    )

def execute_shell_command(command: str) -> Dict:
    global CURRENT_CWD
    print(f"\n[SYSTEM] Executing in [{to_bash_path(CURRENT_CWD)}]: \033[38;5;112m{command}\033[0m")
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as tf:
        cwd_file = tf.name
    bash_cwd_file = to_bash_path(cwd_file)
    
    wrapped_command = f"trap 'pwd > \"{bash_cwd_file}\"' EXIT\n{command}"
    cmd_args = ["bash", "-c", wrapped_command]
    
    proc = None
    execbin = None
    if os.environ.get('OS') == 'Windows_NT':
        execbin = r"C:\Program Files\Git\bin\bash.exe"  
                
    try:
        proc = subprocess.Popen(
            cmd_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=CURRENT_CWD,
            executable=execbin)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=30)
            
            if os.path.exists(cwd_file):
                try:
                    with open(cwd_file, 'r', encoding='utf-8') as f:
                        captured_path = f.read().strip()
                    if captured_path:
                        new_cwd = resolve_path(captured_path)
                        if os.path.isdir(new_cwd):
                            if new_cwd != CURRENT_CWD:
                                print(f"\033[33m[CWD CHANGED]\033[0m {to_bash_path(CURRENT_CWD)} -> {to_bash_path(new_cwd)}")
                            CURRENT_CWD = new_cwd
                except Exception as e:
                    print(f"[SYSTEM] Warning: Could not parse updated directory: {e}")
                    
            return {"exit_code": proc.returncode, "stdout": stdout_data.strip(), "stderr": stderr_data.strip()}
        except subprocess.TimeoutExpired:
            print(f"[SYSTEM] Timeout reached (30s). Forcing termination (SIGKILL).")
            proc.kill() 
            proc.wait() 
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out."}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Error: {str(e)}"}
    finally:
        if proc and proc.poll() is None:
             proc.kill()
        if os.path.exists(cwd_file):
            try:
                os.unlink(cwd_file)
            except Exception:
                pass

def run_agent_loop(user_intent: str, memory_manager: UvianMemoryManager, max_turns: int = 8, pinned_context: Optional[str] = None) -> Optional[str]:
    global CURRENT_CWD, LAST_UUID
    
    context_injection = ""
    target_uuid = None
    
    # 1. Bypass candidate matching entirely if a UUID is pinned
    if pinned_context:
        target_uuid = pinned_context
        print(f"\n📌 Bypassing matching phase. Using Pinned UUID: {target_uuid}")
    else:
        print("\n🔍 Searching Uvian memory entries using text matching...")
        match = memory_manager.retrieve_most_relevant_uuid(user_intent)
        
        if match:
            target_uuid, summary = match
            print(f"🎯 LLM Match found! UUID: {target_uuid}")
        else:
            print("ℹ️ No relevant Uvian UUID entries found.")

    if target_uuid:
        LAST_UUID = target_uuid
        res = handle_uvian_entry(target_uuid, args.uvian_dir)
        if res["type"] == "tool_execution":
            context_injection = (
                f"\n\n[AUTOMATIC UVIAN TOOL EXECUTION]\n"
                f"Executed UUID File: {target_uuid} via engine '{res['engine']}'\n"
                f"Exit Code: {res['exit_code']}\n"
                f"Stdout: {res['stdout']}\n"
                f"Stderr: {res['stderr']}"
            )
        elif res["type"] == "error":
            print(f"⚠️ Uvian Processing Error: {res['content']}")

    os_context = get_os_env_context()
    enriched_intent = f"User Intent: {user_intent}{os_context}{context_injection}"
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": enriched_intent}
    ]
    
    mr_tool = VRAMOptimizedMapReduceAgent(llm_call=standard_llm_call, max_chars_per_chunk=args.chunk_size)

    for turn in range(max_turns):
        print(f"\n\033[36m\033[1m---- Turn {turn + 1} ----\033[0m")        
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        llm_output = response.choices[0].message.content
        parsed_action = parse_llm_json(llm_output)
        messages.append({"role": "assistant", "content": json.dumps(parsed_action)})

        action_type = parsed_action.get("action_type")
        
        if action_type == "declare_result":
            print(f"\n❇  [FINAL RESULT] {parsed_action.get('status')}")
            print(f"🧠 [REASON] {parsed_action.get('reason')}")
            return target_uuid # Return to main loop for pinning
            
        elif action_type == "run_command":
            command = parsed_action.get("command")
            print(f"\n\033[31m⚠️ [ACTION REQUIRED]\033[0m Agent wants to execute:\n  > \033[1m{command}\033[0m")
            
            if is_autopilot_enabled():
                print("\033[33m⚡ [AUTOPILOT]\033[0m Command automatically approved via ~/echo-home/autopilot.yaml")
                approval = 'y'
            else:
                approval = input("Allow? (y=yes / n=no / e=edit): ").strip().lower()
                if approval == 'e':
                    command = input("Edit command: ")
                    approval = 'y'
                
            if approval != 'y':
                print("🚫 [SYSTEM] Command denied.")
                messages.append({"role": "user", "content": f"Execution DENIED by user for command: {command}."})
                continue
                
            exec_result = execute_shell_command(command)
            
            print(f"\n\033[32m[EXIT CODE]\033[0m {exec_result['exit_code']}")
            if exec_result['stdout']:
                print(f"\033[32m[STDOUT]\033[0m\n{exec_result['stdout']}")
            if exec_result['stderr']:
                print(f"\033[31m[STDERR]\033[0m\n{exec_result['stderr']}")
            print("-" * 40)
            
            final_stdout = mr_tool.run(user_intent, exec_result['stdout'])
            final_stderr = mr_tool.run(user_intent, exec_result['stderr'])
            
            evidence = (
                f"Command Executed: {command}\n"
                f"Current Working Directory: {to_bash_path(CURRENT_CWD)}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {final_stdout}\n"
                f"stderr: {final_stderr}"
            )
            messages.append({"role": "user", "content": evidence})
            
        else:
            print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

    print("\n⚠️ [WARNING] Max turns reached. Loop terminated.")
    return target_uuid # Return to main loop for pinning

# ==========================================
# 6. ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not MODEL_NAME:
        exit(1)
        
    memory = UvianMemoryManager(
        host=args.host, 
        port=args.port,
        model_name=MODEL_NAME,
        uvian_dir=args.uvian_dir
    )
    
    memory.sync_yaml_to_chroma(args.summary)
        
    print("🚀 Memphia Agentic Loop Initialized. Type 'exit' to quit.")
    print(f"🖥️  Detected System: {get_os_env_context()}")
    promptia_session = PromptSession()
    promptia_style = Style.from_dict({
        'llm': 'bg:#c4c408 fg:#000000 bold',
        'prompt': 'bg:#000000 fg:#c4c408',
        'ws': 'bg:#c4c408 fg:#c4c408'
    })
    
    pinned_uuid = None
    pinned_content = None
    
    while True:
        try:
            user_input = promptia_session.prompt(
                    [('class:llm', ' MEMPHIA '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=promptia_style
            )
            
            clean_input = user_input.strip()
            
            if clean_input.lower() in ['exit', 'quit']:
                break

            if clean_input == '===l':
                if LAST_UUID:
                    print(f"📌 Memory pinned at {LAST_UUID}")
                    pinned_uuid = LAST_UUID
                else:
                    print("ℹ️ No last uuid is found to be pinned.")
                continue

            if clean_input == '====':
                if pinned_uuid:
                    print(f"🔓 Unpinned memory ({pinned_uuid}). Exited UUID pinning mode.")
                    pinned_uuid = None
                    pinned_content = None
                else:
                    print("ℹ️ No memory file is currently pinned.")
                continue

            if clean_input.startswith('=-='):
                search_query = clean_input[3:].strip()
                if not search_query:
                    print("⚠️ Please enter keywords after '=-='")
                    continue
                
                words = search_query.split()
                
                # Compile word-boundary regexes for each individual word (ANY match logic)
                patterns = []
                for w in words:
                    try:
                        patterns.append(re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE))
                    except Exception:
                        pass
                
                all_memories = memory.collection.get()
                ids = all_memories.get('ids', [])
                documents = all_memories.get('documents', [])
                
                yaml_lines = []
                
                for doc_id, doc_text in zip(ids, documents):
                    matching_lines = [
                        line.strip() for line in doc_text.splitlines() 
                        if any(p.search(line) for p in patterns)
                    ]
                    if matching_lines:
                        yaml_lines.append(f"\033[44m\033[97m{doc_id}\033[0m: |")
                        for line in matching_lines:
                            yaml_lines.append(f"    {line}")
                
                if yaml_lines:
                    print("```yaml")
                    print("\n".join(yaml_lines))
                    print("```")
                    
                    picked = input("\n📌 Enter UUID file to manually pick/pin (or press Enter to skip): ").strip()
                    if picked in ids:
                        pinned_uuid = picked
                        pinned_content = documents[ids.index(picked)]
                        print(f"✅ Memory {pinned_uuid} pinned! It will be used in subsequent prompts until you type '===='.")
                    elif picked:
                        print("❌ UUID not found. Proceeding without pinning.")
                else:
                    print("🔍 No matches found in memory.")
                
                continue

            if clean_input:
                found_uuid = run_agent_loop(user_input, memory, pinned_context=pinned_uuid)
                
                # Auto-pin the UUID if the LLM selected one and we don't currently have a pin active
                if found_uuid and not pinned_uuid:
                    pinned_uuid = found_uuid
                    print(f"\n✅ Auto-pinned memory UUID {pinned_uuid} for the rest of this session to save search costs.")
                    print("   Type '====' anytime to unpin and allow new memory searches.")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
