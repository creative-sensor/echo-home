.PHONY: help ast architect microtask merge run clean reset check-script check-prompt inspect

# Default variables
HOST ?= localhost
PORT ?= 8080

# Extract the main port safely for other scripts (e.g. architect.py) that only expect an integer
MAIN_PORT = $(shell echo $(PORT) | cut -d',' -f1)

# Conditionally add debug flag if DEBUG=true
ifeq ($(DEBUG), true)
  DEBUG_FLAG = --debug
else
  DEBUG_FLAG = 
endif

# Dynamic paths based on the provided script
SCRIPT_DIR = $(dir $(SCRIPT))
SCRIPT_NAME = $(notdir $(SCRIPT))
META_DIR = $(SCRIPT_DIR).$(SCRIPT_NAME)
AST_FILE = $(META_DIR)/ast.yaml
ARCHER_FILE = $(META_DIR)/archer.yaml

# --- ANSI Foreground & Background Colors ---
RESET         := \033[0m

# Syntax: \033[<BOLD>;<FOREGROUND>;<BACKGROUND>m
STAGE1_COLOR  := \033[1;30;46m# Bold Black text on Cyan background
STAGE2_COLOR  := \033[1;30;43m# Bold Black text on Yellow background
STAGE3_COLOR  := \033[1;30;45m# Bold White text on Magenta background
STAGE4_COLOR  := \033[1;30;42m# Bold Black text on Green background
STAGE5_COLOR  := \033[1;30;44m# Bold Black text on Blue background
SUCCESS_COLOR := \033[1;37;42m# Bold White text on Green background
ERROR_COLOR   := \033[1;37;41m# Bold White text on Red background
RESET_COLOR   := \033[1;37;41m# Bold White text on Red background

help:
	@echo -e "\033[1mMicrotasker Pipeline Makefile\033[0m"
	@echo -e "-----------------------------"
	@echo -e "Usage:"
	@echo -e "  make run SCRIPT=<path/to/script.py> PROMPT=\"<your instruction>\" [DEBUG=true] [HOST=...] [PORT=...]"
	@echo -e ""
	@echo -e "Individual Stages:"
	@echo -e "  make ast SCRIPT=<script.py>                      - Generate AST YAML"
	@echo -e "  make architect SCRIPT=<script.py>                - Generate high-level Architecture YAML"
	@echo -e "  make microtask SCRIPT=<script.py> PROMPT=\"...\"   - Run agents to modify components"
	@echo -e "  make merge SCRIPT=<script.py> [PROMPT=\"...\"]     - Patch original script (runs microtask if PROMPT is given)"
	@echo -e "  make reset SCRIPT=<script.py>                    - Hard reset by completely wiping the meta directory"



# --- Safety Checks ---

check-script:
	@if [ -z "$(SCRIPT)" ]; then \
		echo -e "$(ERROR_COLOR) [!] Error: SCRIPT variable is required. Example: SCRIPT=main.py $(RESET)"; \
		echo "$(ERROR_COLOR) [!] Error: SCRIPT variable is required. Example: SCRIPT=main.py $(RESET)"; \
		exit 1; \
	fi

check-prompt:
	@if [ -z "$(PROMPT)" ] && [ -z "$(PROMPT64)" ]; then \
		echo -e "$(ERROR_COLOR) [!] Error: PROMPT or PROMPT64 variable is required. $(RESET)"; \
		exit 1; \
	fi

# --- Pipeline Steps ---
# Route to the correct python argument based on which variable is provided
microtask: check-script check-prompt $(ARCHER_FILE)
	@echo  ""
	@echo -e "$(STAGE3_COLOR) ======================================== $(RESET)"
	@echo -e "$(STAGE3_COLOR) [STAGE 3] Executing Microtasks...        $(RESET)"
	@echo -e "$(STAGE3_COLOR) ======================================== $(RESET)"
	@if [ -n "$(PROMPT64)" ]; then \
		python microtasker.py --script "$(SCRIPT)" --prompt64 "$(PROMPT64)" --host $(HOST) --port $(PORT) $(DEBUG_FLAG); \
	else \
		python microtasker.py --script $(SCRIPT) --prompt "$(PROMPT)" --host $(HOST) --port $(PORT) $(DEBUG_FLAG); \
	fi

# Trigger microtask if either PROMPT or PROMPT64 is provided
merge: check-script $(if $(or $(PROMPT),$(PROMPT64)), microtask)
	@echo  ""
	@echo -e "$(STAGE4_COLOR) ======================================== $(RESET)"
	@echo -e "$(STAGE4_COLOR) [STAGE 4] Merging Changes & Selective... $(RESET)"
	@echo -e "$(STAGE4_COLOR) ======================================== $(RESET)"
	python merge.py $(SCRIPT)
	python mkast.py -f $(SCRIPT)
	python architect.py --input $(AST_FILE) --output $(ARCHER_FILE) --host $(HOST) --port $(MAIN_PORT) $(DEBUG_FLAG) --selective
	@echo -e "\033[1m[*] Cleaning up applied microtasks...\033[0m"
	rm -f $(META_DIR)/FunctionDef.* $(META_DIR)/ClassDef.* $(META_DIR)/Module.* $(META_DIR)/Unknown.* 
	@touch $(AST_FILE)
	@touch $(ARCHER_FILE)

# --- File Dependencies (Auto-Resolution) ---

# Rebuild ast.yaml if the Python script has been modified, or if it doesn't exist
$(AST_FILE): $(SCRIPT) check-script
	@echo -e ""
	@echo -e "$(STAGE1_COLOR) ======================================== $(RESET)"
	@echo -e "$(STAGE1_COLOR) [STAGE 1] Extracting AST...              $(RESET)"
	@echo -e "$(STAGE1_COLOR) ======================================== $(RESET)"
	python mkast.py -f $(SCRIPT)

# Trigger architect script (will bypass LLM if file exists, unless --selective is used during merge)
$(ARCHER_FILE): $(AST_FILE) check-script
	@echo -e ""
	@echo -e "$(STAGE2_COLOR) ======================================== $(RESET)"
	@echo -e "$(STAGE2_COLOR) [STAGE 2] Generating Architecture...     $(RESET)"
	@echo -e "$(STAGE2_COLOR) ======================================== $(RESET)"
	python architect.py --input $(AST_FILE) --output $(ARCHER_FILE) --host $(HOST) --port $(MAIN_PORT) $(DEBUG_FLAG)

# --- Pipeline Steps (Phony Aliases) ---

ast: $(AST_FILE)

architect: $(ARCHER_FILE)


# --- Independent Inspect Stage ---
# Removed 'merge' so this can be triggered on its own using `make inspect SCRIPT=... PROMPT=...`
inspect: check-script check-prompt
	@echo  ""
	@echo -e "$(STAGE5_COLOR) ======================================== $(RESET)"
	@echo -e "$(STAGE5_COLOR) [STAGE 5] Inspecting Execution...        $(RESET)"
	@echo -e "$(STAGE5_COLOR) ======================================== $(RESET)"
	python inspector.py --script $(SCRIPT) --prompt "$(PROMPT)" --host $(HOST) --port $(PORT) $(DEBUG_FLAG)



# --- All-in-One ---
run: microtask merge inspect
	@echo  ""
	@echo -e "$(SUCCESS_COLOR) ======================================== $(RESET)"
	@echo -e "$(SUCCESS_COLOR) [+] Pipeline completed for $(SCRIPT)! $(RESET)"
	@echo -e "$(SUCCESS_COLOR) ======================================== $(RESET)"

# --- Cleanup / Hard Reset ---
reset: check-script
	@echo -e "$(RESET_COLOR) [*] Hard resetting meta directory for $(SCRIPT) (wiping all AST and architecture data)... $(RESET)"
	rm -rf $(META_DIR)

# Alias for backwards compatibility if you used 'make clean' previously
clean: reset
