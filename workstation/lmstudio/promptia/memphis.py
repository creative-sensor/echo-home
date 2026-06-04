#!/usr/bin/env python
import os
import json
import subprocess
import re
import argparse
import textwrap
import requests
import uuid
from typing import Optional, Dict, List, Callable

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style

try:
    import chromadb
except ImportError:
    print("❌ ERROR: Missing dependencies. Run: pip install chromadb")
    exit(1)

# ==========================================
# 1. CLI ARGUMENTS & API SETUP
# ==========================================
parser = argparse.ArgumentParser(description="Connect to a local LLM API endpoint with RAG memory.")
parser.add_argument('--port', type=int, default=8080, help='The port number of the local LLM server')
parser.add_argument('--host', type=str, default='localhost', help='The hostname of the local LLM server')
parser.add_argument('--chunk-size', type=int, default=30000, help='Max characters before Map-Reduce is triggered')
args = parser.parse_args()

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
# 2. RAG MEMORY MANAGER
# ==========================================
class RagMemoryManager:
    def __init__(self, host: str = 'localhost', port: int = 8080):
        print("🧠 Initializing RAG Memory Database...")
        # Saves the database locally in the current directory
        self.chroma_client = chromadb.PersistentClient(path="./memphis/vector")
        self.collection = self.chroma_client.get_or_create_collection(name="shell_sessions")
        
        print(f"   -> Using llama.cpp embedding endpoint at http://{host}:{port}/v1")
        self.openai_client = OpenAI(base_url=f"http://{host}:{port}/v1", api_key="localm")

    def get_embedding(self, text: str) -> list[float]:
        """Generates an embedding vector using the llama.cpp API."""
        # Note: Ensure your llama.cpp server is launched with an embedding model
        response = self.openai_client.embeddings.create(input=[text], model="local-model")
        return response.data[0].embedding

    def retrieve_context(self, user_intent: str, top_k: int = 2) -> str:
        """Searches past sessions for similar tasks."""
        if self.collection.count() == 0:
            return "No previous memories exist yet."
            
        query_embedding = self.get_embedding(user_intent)
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.collection.count())
        )
        
        if not results['documents'] or not results['documents'][0]:
            return "No highly relevant past memories found."
            
        return "\n\n---\n\n".join(results['documents'][0])

    def store_session(self, user_intent: str, summary: str):
        """Saves a successfully completed task to the database."""
        session_id = str(uuid.uuid4())
        document_text = f"Original Intent: {user_intent}\nSolution Summary:\n{summary}"
        embedding = self.get_embedding(document_text)
        
        self.collection.add(
            ids=[session_id],
            embeddings=[embedding],
            documents=[document_text],
            metadatas=[{"intent": user_intent}]
        )
        print(f"💾 Memory stored permanently! (ID: {session_id[:8]}...)")

# ==========================================
# 3. STANDARD LLM CALL HANDLER
# ==========================================
def standard_llm_call(system_prompt: str, user_prompt: str) -> str:
    """A standard synchronous call to the LLM used by the Map-Reduce agent and summarizer."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.0
    )
    return response.choices[0].message.content

# ==========================================
# 4. MAP-REDUCE CAPABILITY
# ==========================================
class VRAMOptimizedMapReduceAgent:
    def __init__(self, llm_call: Callable[[str, str], str], max_chars_per_chunk: int):
        self.llm_call = llm_call
        self.max_chars_per_chunk = max_chars_per_chunk

    def _chunk_text(self, text: str) -> List[str]:
        return textwrap.wrap(text, width=self.max_chars_per_chunk, break_long_words=False)

    def run(self, task_description: str, input_data: str) -> str:
        if len(input_data) <= self.max_chars_per_chunk:
            return input_data 

        print(f"\n\033[35m[MAP-REDUCE]\033[0m Output too large ({len(input_data)} chars). Triggering summarization...")
        chunks = self._chunk_text(input_data)
        chunk_results = []
        
        map_system_prompt = (
            "You are a command-line data extraction agent. "
            "Extract and summarize ONLY the information relevant to the user's current task. "
            "If the chunk contains no relevant information, reply exactly with 'NO RELEVANT INFO'."
        )
        
        for i, chunk in enumerate(chunks):
            print(f"  -> Processing chunk {i + 1}/{len(chunks)}...")
            user_prompt = f"User Intent/Task: {task_description}\n\nChunk Output Data:\n{chunk}"
            result = self.llm_call(map_system_prompt, user_prompt)
            
            if "NO RELEVANT INFO" not in result.upper():
                chunk_results.append(f"--- Info from chunk {i+1} ---\n{result}")

        print("  -> Consolidating final summary...")
        consolidated_context = "\n\n".join(chunk_results)
        
        if not consolidated_context.strip():
            return "Map-Reduce resulted in no relevant findings based on the user intent."

        reduce_system_prompt = (
            "You are a synthesis agent. You are given partial summaries extracted from a massive "
            "terminal output. Consolidate them into a final, coherent summary that answers the user's intent."
        )
        reduce_user_prompt = f"Original Task: {task_description}\n\nExtracted Findings:\n{consolidated_context}"
        
        return self.llm_call(reduce_system_prompt, reduce_user_prompt)

# ==========================================
# 5. SHELL AGENT CORE
# ==========================================
SYSTEM_PROMPT = """<|think|>
You are an interactive shell operator. You have access to a terminal. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

RULES:
1. You operate in a loop. You can issue a shell command, ask the user for clarification, or declare the task finished.
2. Do not declare SUCCESS unless you have explicit proof from stdout/stderr.
3. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_command" or "ask_user" or "declare_result",
  "command": "<shell command> or null",
  "question": "<question to ask user> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for result> or null"
}
"""

def execute_shell_command(command: str) -> Dict:
    print(f"\n[SYSTEM] Executing: {command}")
    cmd_args = ["bash", "-c", command]
    execbin = r"C:\Program Files\Git\bin\bash.exe" if os.environ.get('OS') == 'WINDOW_NT' else None     
    proc = None
    
    try:
        proc = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, executable=execbin)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=30)
            return {"exit_code": proc.returncode, "stdout": stdout_data.strip(), "stderr": stderr_data.strip()}
        except subprocess.TimeoutExpired:
            print(f"[SYSTEM] Timeout reached (30s). Forcing termination (SIGKILL).")
            proc.kill() 
            proc.wait() 
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out and was forcefully terminated."}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"Error: {str(e)}"}
    finally:
        if proc and proc.poll() is None:
             proc.kill()

def parse_llm_json(raw_text: str) -> dict:
    # 1. Strip Gemma 4's internal reasoning block
    clean_text = re.sub(r"<\|channel>thought.*?<channel\|>", "", raw_text, flags=re.DOTALL)
    
    # 2. Strip markdown formatting
    clean_text = re.sub(r"```json", "", clean_text)
    clean_text = re.sub(r"```", "", clean_text).strip()
    
    try:
        return json.loads(clean_text)
    except json.JSONDecodeError:
        return {"action_type": "declare_result", "status": "FAILED", "reason": "LLM output invalid JSON."}

def run_agent_loop(user_intent: str, memory_manager: RagMemoryManager, max_turns: int = 8):
    print("\n🔍 Searching long-term memory for relevant past contexts...")
    past_knowledge = memory_manager.retrieve_context(user_intent)
    
    enriched_intent = (
        f"User Intent: {user_intent}\n\n"
        f"Past Relevant Knowledge/Solutions:\n{past_knowledge}"
    )
    
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
        
        # We append the cleaned JSON, not the raw thoughts, keeping context clean
        messages.append({"role": "assistant", "content": json.dumps(parsed_action)})

        action_type = parsed_action.get("action_type")
        
        if action_type == "declare_result":
            print(f"\n❇  [FINAL RESULT] {parsed_action.get('status')}")
            print(f"🧠 [REASON] {parsed_action.get('reason')}")
            
            # RAG WRITE PHASE
            if parsed_action.get('status') == "SUCCESS":
                print("\n📝 Summarizing successful session for memory storage...")
                
                history = "\n".join([m['content'] for m in messages if m['role'] != 'system'])
                summary_prompt = "Summarize the successful shell commands used to achieve the user's intent. Do not include failures, just the final working path."
                
                session_summary = standard_llm_call(summary_prompt, history)
                memory_manager.store_session(user_intent, session_summary)
                
            return
            
        elif action_type == "ask_user":
            print(f"\n\033[33m🤔 [AGENT ASKS]\033[0m {parsed_action.get('question')}")
            user_response = input("Your answer: ")
            messages.append({"role": "user", "content": f"User replied: {user_response}"})
            
        elif action_type == "run_command":
            command = parsed_action.get("command")
            
            print(f"\n\033[31m⚠️ [ACTION REQUIRED]\033[0m Agent wants to execute:\n  > \033[1m{command}\033[0m")
            approval = input("Allow? (y=yes / n=no / e=edit): ").strip().lower()
            
            if approval == 'e':
                command = input(f"Edit command: ")
                approval = 'y'
                
            if approval != 'y':
                print("🚫 [SYSTEM] Command denied.")
                messages.append({"role": "user", "content": f"Execution DENIED by user for command: {command}."})
                continue
                
            exec_result = execute_shell_command(command)
            
            final_stdout = mr_tool.run(user_intent, exec_result['stdout'])
            final_stderr = mr_tool.run(user_intent, exec_result['stderr'])
            
            print(f"[EXIT CODE] {exec_result['exit_code']}")
            if final_stdout: print(f"[STDOUT (Processed)]\n{final_stdout}")
            if final_stderr: print(f"[STDERR (Processed)]\n{final_stderr}")
            
            evidence = (
                f"Command Executed: {command}\n"
                f"Exit Code: {exec_result['exit_code']}\n"
                f"stdout: {final_stdout}\n"
                f"stderr: {final_stderr}"
            )
            messages.append({"role": "user", "content": evidence})
            
        else:
            print(f"\n❌ [ERROR] Unknown action type: {action_type}")
            break

    print("\n⚠️ [WARNING] Max turns reached. Loop terminated.")

# ==========================================
# 6. ENTRY POINT
# ==========================================
if __name__ == "__main__":
    if not MODEL_NAME:
        exit(1)
        
    memory = RagMemoryManager(
        host=args.host, 
        port=args.port
    )
        
    print("🚀 VRAM-Optimized Interactive LLM CLI Agent Initialized. Type 'exit' to quit.")
    promptia_session = PromptSession()
    promptia_style = Style.from_dict({
        'llm': 'bg:#c4c408 fg:#000000 bold',
        'prompt': 'bg:#000000 fg:#c4c408',
        'ws': 'bg:#c4c408 fg:#c4c408'
    })
    
    while True:
        try:
            user_input = promptia_session.prompt(
                    [('class:llm', ' MEMPHIS '), ('class:prompt', ' Prompt!a '), ('class:ws', ' ')],
                    multiline=True,
                    style=promptia_style
            )
            if user_input.strip().lower() in ['exit', 'quit']:
                break
            if user_input.strip():
                run_agent_loop(user_input, memory)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
