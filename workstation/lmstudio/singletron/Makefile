.PHONY: help ast architect microtask merge run clean reset check-script check-prompt

# Default variables
HOST ?= localhost
PORT ?= 8080

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

help:
	@echo "Microtasker Pipeline Makefile"
	@echo "-----------------------------"
	@echo "Usage:"
	@echo "  make run SCRIPT=<path/to/script.py> PROMPT=\"<your instruction>\" [DEBUG=true] [HOST=...] [PORT=...]"
	@echo ""
	@echo "Individual Stages:"
	@echo "  make ast SCRIPT=<script.py>             - Generate AST YAML"
	@echo "  make architect SCRIPT=<script.py>       - Generate high-level Architecture YAML"
	@echo "  make microtask SCRIPT=<script.py> PROMPT=\"...\" - Run agents to modify components"
	@echo "  make merge SCRIPT=<script.py>           - Patch the original script and resync architecture"
	@echo "  make reset SCRIPT=<script.py>           - Hard reset by completely wiping the meta directory"

# --- Safety Checks ---
check-script:
	@if [ -z "$(SCRIPT)" ]; then \
		echo "[!] Error: SCRIPT variable is required. Example: SCRIPT=main.py"; \
		exit 1; \
	fi

check-prompt:
	@if [ -z "$(PROMPT)" ]; then \
		echo "[!] Error: PROMPT variable is required for microtasks. Example: PROMPT=\"Update the function...\""; \
		exit 1; \
	fi

# --- File Dependencies (Auto-Resolution) ---

# Rebuild ast.yaml if the Python script has been modified, or if it doesn't exist
$(AST_FILE): $(SCRIPT) check-script
	@echo "\n========================================"
	@echo "[STAGE 1] Extracting AST..."
	@echo "========================================"
	python mkast.py -f $(SCRIPT)

# Trigger architect script (will bypass LLM if file exists, unless --selective is used during merge)
$(ARCHER_FILE): $(AST_FILE) check-script
	@echo "\n========================================"
	@echo "[STAGE 2] Generating Architecture Report..."
	@echo "========================================"
	python architect.py --input $(AST_FILE) --output $(ARCHER_FILE) --host $(HOST) --port $(PORT) $(DEBUG_FLAG)


# --- Pipeline Steps (Phony Aliases) ---

ast: $(AST_FILE)

architect: $(ARCHER_FILE)

# Microtask strictly requires the Architecture report to exist first
microtask: check-script check-prompt $(ARCHER_FILE)
	@echo "\n========================================"
	@echo "[STAGE 3] Executing Microtasks..."
	@echo "========================================"
	python microtasker.py --script $(SCRIPT) --prompt "$(PROMPT)" --host $(HOST) --port $(PORT) $(DEBUG_FLAG)

merge: check-script
	@echo "\n========================================"
	@echo "[STAGE 4] Merging Changes & Selective Architect Update..."
	@echo "========================================"
	python merge.py $(SCRIPT)
	python mkast.py -f $(SCRIPT)
	python architect.py --input $(AST_FILE) --output $(ARCHER_FILE) --host $(HOST) --port $(PORT) $(DEBUG_FLAG) --selective
	@echo "[*] Cleaning up applied microtasks..."
	rm -f $(META_DIR)/FunctionDef.* $(META_DIR)/ClassDef.* $(META_DIR)/Module.*
	@touch $(AST_FILE)
	@touch $(ARCHER_FILE)

# --- All-in-One ---
run: microtask merge
	@echo "\n========================================"
	@echo "[+] Pipeline completed successfully for $(SCRIPT)!"
	@echo "========================================"

# --- Cleanup / Hard Reset ---
reset: check-script
	@echo "[*] Hard resetting meta directory for $(SCRIPT) (wiping all AST and architecture data)..."
	rm -rf $(META_DIR)

# Alias for backwards compatibility if you used 'make clean' previously
clean: reset
