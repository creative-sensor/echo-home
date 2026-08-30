#!/usr/bin/env python
#!/usr/bin/env python
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import concurrent.futures
import datetime
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

ADR_FILE = "architectural_decisions.json"
HISTORY_FILE = ".design_cli_history"
MAX_CONSENSUS_ROUNDS = 3


# --- Endpoint Configuration & Trilogy Parsing ---

@dataclass
class Endpoint:
    host: str
    port: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

@dataclass
class TeamEndpoints:
    analyst: Endpoint
    architect: Endpoint
    subagent: Endpoint

@dataclass
class ADR:
    id: str
    title: str
    status: str
    context: str
    decision: str
    consequences: str


def parse_trilogy(raw_input: Optional[str], default_fallback: str) -> Tuple[str, str, str]:
    if not raw_input:
        return default_fallback, default_fallback, default_fallback
    parts = [p.strip() for p in raw_input.split(',')]
    while len(parts) < 3:
        parts.append('')
    analyst = parts[0] if parts[0] else default_fallback
    architect = parts[1] if parts[1] else analyst
    subagent = parts[2] if parts[2] else analyst
    return analyst, architect, subagent


# --- Server Health Check ---

def check_endpoints_health(endpoints: TeamEndpoints):
    print("Meeting Setup: ")
    
    roles = [
        ("Business Analyst", endpoints.analyst),
        ("Head Architect", endpoints.architect),
        ("Specialist Subagent", endpoints.subagent)
    ]
    
    all_healthy = True
    seen_urls = {}

    for role_name, ep in roles:
        if ep.base_url in seen_urls:
            print(f"  [\u2713] {seen_urls[ep.base_url]} as {role_name}")
            continue

        try:
            req = urllib.request.Request(f"{ep.base_url}/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    model_name = "Unknown_Model"
                    if "data" in data and len(data["data"]) > 0:
                        model_name = data["data"][0].get("id", "Unknown_Model")
                    
                    seen_urls[ep.base_url] = model_name
                    print(f"  [\u2713] {model_name} as {role_name}")
                else:
                    print(f"  [X] Failed to verify {role_name} (HTTP {response.status})")
                    all_healthy = False
        except Exception as e:
            print(f"  [X] Error connecting to {role_name} at {ep.base_url} ({e})")
            all_healthy = False
            
    if not all_healthy:
        print("\n⚠️ Fatal Error: One or more LLM endpoints are unreachable. Exiting...")
        sys.exit(1)


# --- Meeting Logger ---

class MeetingLogger:
    def __init__(self, project_dir: str):
        now = datetime.datetime.now()
        self.filename = os.path.join(project_dir, now.strftime("meeting-%Y-%m-%d--%H-%M.md"))
        with open(self.filename, "w", encoding="utf-8") as f:
            f.write(f"# Software Design Meeting\nDate: {now.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        print(f"\n📝 Meeting transcript will be saved to: {self.filename}\n")

    def log_turn(self, user_input: str, results: Dict[str, Any]):
        with open(self.filename, "a", encoding="utf-8") as f:
            f.write(f"## User Request\n> {user_input}\n\n")
            f.write(f"### Business Analyst Note\n{results['ba']}\n\n")
            f.write(f"### Head Architect Note\n{results['ha']}\n\n")
            if results.get('subagent'):
                f.write(f"### Specialist Advice\n{results['subagent']}\n\n")
            f.write(f"### Team Consensus & Proposal\n**{results['consensus']}**\n\n")
            f.write("---\n\n")

    def log_summary(self, summary: str):
        with open(self.filename, "a", encoding="utf-8") as f:
            f.write(f"## Meeting Key Takeaways & Notes\n{summary}\n")


# --- Persistent Memory Engine ---

class PersistentMemory:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.adrs: List[ADR] = self.load_adrs()

    def load_adrs(self) -> List[ADR]:
        if not os.path.exists(self.storage_path): return []
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                return [ADR(**item) for item in json.load(f)]
        except Exception: return []

    def save_adrs(self):
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump([asdict(adr) for adr in self.adrs], f, indent=2)

    def add_adr(self, title: str, context: str, decision: str, consequences: str) -> ADR:
        adr = ADR(f"ADR-{len(self.adrs) + 1:03d}", title, "Accepted", context, decision, consequences)
        self.adrs.append(adr)
        self.save_adrs()
        return adr

    def get_summary(self) -> str:
        if not self.adrs: return "No prior ADRs recorded."
        return "\n".join([f"[{a.id}] {a.title}: {a.decision}" for a in self.adrs])


# --- LLM Communication & Isolated Agent Sessions ---

class LLMClient:
    def __init__(self):
        self._clients: Dict[str, OpenAI] = {}

    def _get_client(self, base_url: str) -> OpenAI:
        if base_url not in self._clients:
            self._clients[base_url] = OpenAI(base_url=base_url, api_key="llama.cpp")
        return self._clients[base_url]

    def generate(self, role: str, messages: List[Dict[str, str]], endpoint: Endpoint) -> str:
        client = self._get_client(endpoint.base_url)
        try:
            response = client.chat.completions.create(
                model="default",
                messages=messages,
                temperature=0.4,
                max_tokens=4096 
            )
            content = response.choices[0].message.content
            if not content or content.isspace():
                return f"[WARNING]: {role} returned 0 tokens. Issue with chat formatting."
            return content.strip()
        except Exception as e:
            return f"[{role} Offline]: {e}"

class AgentSession:
    def __init__(self, role: str, instruction: str, endpoint: Endpoint, llm: LLMClient):
        self.role = role
        self.instruction = instruction
        self.endpoint = endpoint
        self.llm = llm
        self.history: List[Dict[str, str]] = []

    def chat(self, user_text: str) -> str:
        if not self.history:
            msg = f"INSTRUCTION: {self.instruction}\n\nINPUT: {user_text}"
        else:
            msg = user_text
            
        self.history.append({"role": "user", "content": msg})
        
        reply = self.llm.generate(self.role, self.history, self.endpoint)
        self.history.append({"role": "assistant", "content": reply})
        
        if len(self.history) > 10:
            self.history = self.history[:2] + self.history[-6:]
            
        return reply

    def chat_stateless(self, user_text: str) -> str:
        msg = f"INSTRUCTION: {self.instruction}\n\nINPUT: {user_text}"
        return self.llm.generate(self.role, [{"role": "user", "content": msg}], self.endpoint)


# --- Parallel Agent Collaboration System ---

class InteractiveDesignTeam:
    def __init__(self, memory: PersistentMemory, llm: LLMClient, endpoints: TeamEndpoints):
        self.memory = memory
        
        self.ba_session = AgentSession(
            "Business Analyst", 
            "You are a Senior Business Analyst. Focus on user goals, operational workflows, and business logic.", 
            endpoints.analyst, llm
        )
        self.ha_session = AgentSession(
            "Head Architect", 
            "You are a Chief Software Architect. Focus on technical components, scalability, and system architecture.", 
            endpoints.architect, llm
        )
        self.eval_session = AgentSession(
            "Consolidator", 
            "You are the Consolidation Evaluator. Review the Analyst's and Architect's perspectives. "
            "If they agree or are compatible, output 'STATUS: CONSENSUS' followed by a unified proposal. "
            "If they conflict or disagree, output 'STATUS: CONFLICT' followed by a brief summary of the disagreement.", 
            endpoints.architect, llm
        )
        self.subagent_session = AgentSession("Specialist", "You are a technical specialist.", endpoints.subagent, llm)
        
        self.last_consensus = ""
        self.meeting_history_log: List[Tuple[str, str]] = []

    def process_turn(self, user_input: str) -> Dict[str, Any]:
        context = self.memory.get_summary()
        sys_info = f"System ADRs:\n{context}\n\n" if context != "No prior ADRs recorded." else ""
        
        prev_consensus_text = f"Previously agreed and presented to user in last turn:\n{self.last_consensus}\n\n" if self.last_consensus else ""
        
        ba_prompt = f"{sys_info}{prev_consensus_text}New User Request: {user_input}\nProvide your business logic view:"
        ha_prompt = f"{sys_info}{prev_consensus_text}New User Request: {user_input}\nProvide your technical architecture view:"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_ba = executor.submit(self.ba_session.chat, ba_prompt)
            future_ha = executor.submit(self.ha_session.chat, ha_prompt)
            
            ba_view = future_ba.result()
            ha_view = future_ha.result()

        conflict_summary = ""
        consensus_result = ""

        for loop_idx in range(MAX_CONSENSUS_ROUNDS):
            if loop_idx > 0:
                ba_review = (
                    f"The Architect proposed:\n{ha_view}\n\n"
                    f"The Evaluator noted this conflict:\n{conflict_summary}\n\n"
                    f"Propose your next opinion taking the peer's feedback into account."
                )
                ha_review = (
                    f"The Analyst proposed:\n{ba_view}\n\n"
                    f"The Evaluator noted this conflict:\n{conflict_summary}\n\n"
                    f"Propose your next opinion taking the peer's feedback into account."
                )

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    future_ba = executor.submit(self.ba_session.chat, ba_review)
                    future_ha = executor.submit(self.ha_session.chat, ha_review)
                    
                    ba_view = future_ba.result()
                    ha_view = future_ha.result()

            eval_prompt = f"User Request: {user_input}\n\nAnalyst: {ba_view}\n\nArchitect: {ha_view}\n\nEvaluate consensus."
            eval_output = self.eval_session.chat_stateless(eval_prompt)

            if "STATUS: CONSENSUS" in eval_output or loop_idx == MAX_CONSENSUS_ROUNDS - 1:
                consensus_result = eval_output.replace("STATUS: CONSENSUS", "").replace("STATUS: CONFLICT", "").strip()
                if loop_idx > 0:
                    print(f"  \033[92m[Internal Loop {loop_idx+1}] Consensus reached based on alignments.\033[0m")
                break
            else:
                conflict_summary = eval_output.replace("STATUS: CONFLICT", "").strip()
                print(f"  \033[93m[Internal Loop {loop_idx+1}] ⚠️ Conflict detected:\033[0m")
                print(f"  \033[90m> {conflict_summary}\033[0m")
                print("  \033[94m[Re-aligning perspectives...]\033[0m")

        self.last_consensus = consensus_result
        self.meeting_history_log.append((user_input, consensus_result))

        subagent_view = None
        if any(kw in user_input.lower() for kw in ["security", "auth", "encrypt", "database", "sql"]):
            domain = "Security Specialist" if "sec" in user_input.lower() or "auth" in user_input.lower() else "Database Specialist"
            self.subagent_session.instruction = f"You are a {domain}."
            sub_prompt = f"User Request: {user_input}\nAgreed Plan: {consensus_result}\nProvide 1-2 sentences on risks."
            subagent_view = self.subagent_session.chat_stateless(sub_prompt)

        adr = None
        if any(k in user_input.lower() for k in ["must", "adopt", "architecture", "database", "auth", "pivot", "switch"]):
            adr = self.memory.add_adr(
                title=f"Design Rule: {user_input[:30]}...",
                context=f"Request: {user_input}",
                decision=consensus_result[:150] + "...",
                consequences="Updated component boundaries."
            )

        return {
            "ba": ba_view,
            "ha": ha_view,
            "subagent": subagent_view,
            "consensus": consensus_result,
            "adr": adr
        }
        
    def generate_meeting_notes(self) -> str:
        if not self.meeting_history_log:
            return "No discussion was recorded during this meeting."
            
        print("\n\033[94m[Generating Meeting Notes...]\033[0m")
        history_text = "\n\n".join([f"User: {u}\nTeam Consensus: {c}" for u, c in self.meeting_history_log])
        
        prompt = (
            "Based on the following meeting history (User requests and Team consensus outputs), "
            "provide a concise consolidation of Key Takeaways, Decisions Made, and Next Steps.\n\n"
            f"MEETING HISTORY:\n{history_text}"
        )
        
        return self.eval_session.chat_stateless(prompt)


# --- Directory Prompt ---

def prompt_project_selection() -> str:
    base_projects_dir = os.path.join(os.getcwd(), "projects")
    os.makedirs(base_projects_dir, exist_ok=True)
    
    subdirs = sorted([d for d in os.listdir(base_projects_dir) if os.path.isdir(os.path.join(base_projects_dir, d))])
    
    print("\nAvailable projects in ./projects/:")
    if subdirs:
        for idx, d in enumerate(subdirs, 1):
            print(f"  [{idx}] {d}")
    else:
        print("  (No projects found)")
        
    choice = input("\nEnter a new project name, or select a number from above: ").strip()
    
    if not choice:
        print("No project selected. Exiting.")
        sys.exit(1)
        
    if choice.isdigit() and 1 <= int(choice) <= len(subdirs):
        return os.path.join(base_projects_dir, subdirs[int(choice)-1])
    else:
        return os.path.join(base_projects_dir, choice)


# --- Interactive CLI ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=str, default="8090")
    parser.add_argument("--project", type=str, default=None, help="Project folder name (sub-directory) or absolute path.")
    args = parser.parse_args()

    # Determine Project Directory
    if args.project is None:
        project_dir = prompt_project_selection()
        project_name = os.path.basename(project_dir)
        os.makedirs(project_dir, exist_ok=True)
    else:
        if os.path.isdir(os.path.join(os.getcwd(), args.project)):
            project_dir = os.path.join(os.getcwd(), args.project)
            project_name = os.path.basename(project_dir)
        else:
            project_dir = args.project
            project_name = args.project
            os.makedirs(project_dir, exist_ok=True)

    analyst_h, architect_h, subagent_h = parse_trilogy(args.host, "localhost")
    analyst_p, architect_p, subagent_p = parse_trilogy(args.port, "8090")

    endpoints = TeamEndpoints(
        analyst=Endpoint(analyst_h, analyst_p),
        architect=Endpoint(architect_h, architect_p),
        subagent=Endpoint(subagent_h, subagent_p)
    )

    check_endpoints_health(endpoints)
    meeting_logger = MeetingLogger(project_dir)

    memory = PersistentMemory(os.path.join(project_dir, ADR_FILE))
    llm = LLMClient()
    team = InteractiveDesignTeam(memory, llm, endpoints)

    session = PromptSession(
        history=FileHistory(os.path.join(project_dir, HISTORY_FILE)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(['/adrs', '/clear', '===='], ignore_case=True)
    )

    promptia_style = Style.from_dict({'llm': 'bg:#408175 fg:#89D7B7 bold', 'prompt': 'bg:#000000 fg:#89D7B7', 'ws': 'bg:#FFFFFF fg:#89D7B7'})
    print("==================================================")
    print("      SOFTWARE DESIGN TEAM AGENTIC CONSULTANT     ")
    print("==================================================")
    print(f"Project Folder: {project_dir}")
    print("Commands: /adrs | /clear | ==== (End Meeting)\n")

    while True:
        try:
            user_input = session.prompt([('class:llm', ' NEUSTRON '), ('class:prompt', f' {project_name} '), ('class:ws', ' MEET ')], multiline=True, style=promptia_style).strip()

            if not user_input: continue
            
            if user_input == "====":
                print("\nMeeting ended by user.")
                summary = team.generate_meeting_notes()
                meeting_logger.log_summary(summary)
                
                print("\n==================================================")
                print("\033[95m[Meeting Key Takeaways & Notes]")
                print(f"{summary}\033[0m")
                print("==================================================")
                
                print(f"File saved successfully: {meeting_logger.filename}")
                break
                
            if user_input.lower() == "/clear":
                os.system('cls' if os.name == 'nt' else 'clear')
                continue
                
            if user_input.lower() == "/adrs":
                print("\n=== RECORDED ARCHITECTURAL DECISIONS ===")
                print(memory.get_summary())
                print("==================================================\n")
                continue

            results = team.process_turn(user_input)
            meeting_logger.log_turn(user_input, results)

            print("\n==================================================")
            print("\033[48;5;5m\033[38;5;255m [Business Analyst Note] \033[0m")
            print(f"{results['ba']}\n")

            print("\033[48;5;17m\033[38;5;255m [Head Architect Note] \033[0m")
            print(f"{results['ha']}\n")

            if results["subagent"]:
                print("[Specialist Advice]")
                print(f"{results['subagent']}\n")

            print("--------------------------------------------------")
            
            print("\033[48;5;6m\033[38;5;255m [Team Consensus & Proposal] \033[0m")
            print(f"\033[36m{results['consensus']}\033[0m")

            if results["adr"]:
                print(f"\n[ADR RECORDED]: {results['adr'].id} - {results['adr'].title}")
            print()

        except (KeyboardInterrupt, EOFError):
            print("\nSession interrupted.")
            break

if __name__ == "__main__":
    main()
