#!/usr/bin/env node

import fs from 'fs';
import vm from 'vm';
import { parseArgs } from 'util';
import * as readline from 'readline';
import { createRequire } from 'module';
import OpenAI from 'openai';

// Setup require for the VM context so the agent can import native modules (e.g., 'fs', 'path')
const require = createRequire(import.meta.url);

// --- Argument Parsing ---
const options = {
    port: { type: 'string', default: '8080' },
    host: { type: 'string', default: 'localhost' }
};
const { values } = parseArgs({ args: process.argv.slice(2), options });
const port = values.port;
const host = values.host;

// --- Configure Local LLM ---
const client = new OpenAI({
    baseURL: `http://${host}:${port}/v1`,
    apiKey: "localm"
});

async function getModelName(host, port, endpoint) {
    const url = `http://${host}:${port}${endpoint}`;
    try {
        const response = await fetch(url, { signal: AbortSignal.timeout(10000) });
        if (!response.ok) {
            throw new Error(`HTTP Error: ${response.status}`);
        }
        const data = await response.json();
        
        if (data.models && data.models.length > 0 && data.models[0].name) {
            const modelName = data.models[0].name;
            console.log(`✅ Ready: ${modelName}`);
            return modelName;
        } else {
            console.log("⚠️ Warning: Model structure found, but 'name' key is missing or empty.");
            return null;
        }
    } catch (error) {
        console.log(`\n❌ ERROR: Connection Error. \n   ${error.message}`);
        console.log("   Ensure that the API service is running and accessible at the specified host and port.");
        return null;
    }
}

// --- Execution ---

const SYSTEM_PROMPT = `You are an autonomous Node.js/JavaScript agent. Your goal is to fulfill the User Intent, verify it actually worked, and report the final status.

RULES:
1. You operate in a loop. You can either write inline, short, and compact Node.js code to execute/verify something natively, OR you can declare the task finished.
2. The JS code runs in the same persistent VM context across turns. Variables you define in one turn are available in the next. NOTE: Use 'var' or assign to global object to persist variables across turns; top-level 'let'/'const' are scoped only to the current execution block.
3. You have access to 'require' for built-in Node modules (e.g., fs, path, crypto).
4. Do not declare SUCCESS unless you have explicit proof from stdout/stderr. Use console.log() in your code to output results so you can see them.
5. Output ONLY valid JSON. No markdown. No explanations.

OUTPUT SCHEMA:
{
  "action_type": "run_nodejs" or "declare_result",
  "code": "<nodejs code string> or null",
  "status": "SUCCESS" or "FAILED" or null,
  "reason": "<explanation for the result> or null"
}
`;

function executeNodeCode(code, context) {
    console.log(`\n[SYSTEM] Executing Node.js:\n\x1b[38;5;34m${code}\x1b[0m`);
    
    const stdoutTrap = [];
    const stderrTrap = [];
    let exitCode = 0;
    
    // Inject a custom console for this execution turn to capture outputs natively
    context.console = {
        log: (...args) => stdoutTrap.push(args.join(' ')),
        error: (...args) => stderrTrap.push(args.join(' ')),
        warn: (...args) => stdoutTrap.push(args.join(' ')),
        info: (...args) => stdoutTrap.push(args.join(' ')),
        dir: (...args) => stdoutTrap.push(JSON.stringify(args))
    };

    try {
        // Compile and run the code within the persistent context
        const script = new vm.Script(code);
        script.runInContext(context);
    } catch (error) {
        stderrTrap.push(error.stack || error.message);
        exitCode = 1;
    }
        
    return {
        exit_code: exitCode,
        stdout: stdoutTrap.join('\n').trim(),
        stderr: stderrTrap.join('\n').trim()
    };
}

function parseLlmJson(rawText) {
    let cleanText = rawText.replace(/```json/gi, "").replace(/```/g, "").trim();
    try {
        return JSON.parse(cleanText);
    } catch (error) {
        console.log(`[ERROR] Failed to parse JSON from LLM: ${rawText}`);
        return { action_type: "declare_result", status: "FAILED", reason: "LLM output invalid JSON." };
    }
}

async function runAgentLoop(userIntent, modelName, maxTurns = 7) {
    const messages = [
        { role: "system", content: SYSTEM_PROMPT },
        { role: "user", content: `User Intent: ${userIntent}` }
    ];
    
    // Create a persistent Context (Sandbox) for VM executions
    const sandbox = { 
        require: require,
        process: process,
        Buffer: Buffer,
        setTimeout, 
        clearTimeout
    };
    vm.createContext(sandbox);

    for (let turn = 0; turn < maxTurns; turn++) {
        console.log(`\n\x1b[36m\x1b[1m---- Turn ${turn + 1} ----\x1b[0m`);
        
        let response;
        try {
            response = await client.chat.completions.create({
                model: modelName,
                messages: messages,
                temperature: 0.0,
                response_format: { type: "json_object" }
            });
        } catch (error) {
            console.log(`\n❌ [ERROR] LLM Request failed: ${error.message}`);
            break;
        }
        
        const llmOutput = response.choices[0].message.content;
        const parsedAction = parseLlmJson(llmOutput);
        
        messages.push({ role: "assistant", content: JSON.stringify(parsedAction) });

        const actionType = parsedAction.action_type;
        
        if (actionType === "declare_result") {
            const status = parsedAction.status;
            const reason = parsedAction.reason;
            console.log(`\n❇  [FINAL RESULT] ${status}`);
            console.log(`🧠 [REASON] ${reason}`);
            return;
            
        } else if (actionType === "run_nodejs") {
            const code = parsedAction.code;
            if (!code) {
                console.log("\n❌ [ERROR] LLM requested run_nodejs but provided no code.");
                break;
            }
                
            // Execute the JavaScript code in the VM sandbox
            const execResult = executeNodeCode(code, sandbox);
            
            if (execResult.stdout) console.log(`[STDOUT]\n${execResult.stdout}`);
            if (execResult.stderr) console.log(`[STDERR]\n${execResult.stderr}`);
            if (!execResult.stdout && !execResult.stderr) {
                console.log("[OUTPUT] (No standard output or error)");
            }
            
            console.log(`[EXIT CODE] ${execResult.exit_code}`);
            
            // Format the system evidence for the LLM
            const evidence = `Code Executed:\n${code}\nExit Code: ${execResult.exit_code}\nstdout: ${execResult.stdout}\nstderr: ${execResult.stderr}`;
            
            // Feed the evidence back to the LLM for the next turn
            messages.push({ role: "user", content: evidence });
            
        } else {
            console.log(`\n❌ [ERROR] Unknown action type: ${actionType}`);
            break;
        }
    }

    console.log("\n⚠️ [WARNING] Max turns reached. Agent loop terminated to prevent infinite execution.");
}

// --- Main Application Entry ---

async function main() {
    const MODEL_NAME = await getModelName(host, port, "/models");
    if (!MODEL_NAME) {
        process.exit(1);
    }

    console.log("🚀 Local LLM Node.js Agent Initialized. Type 'exit' to quit.");
    console.log("💡 Tip: Multiline mode is active. Press [Enter] for a new line, and [Esc] then [Enter] to submit.\n");

    // Ensure raw keypress events are emitted so we can track the Escape key
    readline.emitKeypressEvents(process.stdin);
    if (process.stdin.isTTY) {
        process.stdin.setRawMode(true);
    }

    const MAIN_PROMPT = '\x1b[48;5;148m\x1b[38;5;232m\x1b[1m NODE-CLINER \x1b[0m\x1b[48;5;232m\x1b[38;5;148m Prompt!a \x1b[0m ';
    const CONTINUATION_PROMPT = '\x1b[90m... \x1b[0m'; // Subtle dots for subsequent lines

    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        prompt: MAIN_PROMPT
    });

    let lines = [];
    let escapePressed = false;

    // Listen natively for the Escape key
    process.stdin.on('keypress', (str, key) => {
        if (key) {
            if (key.name === 'escape') {
                escapePressed = true;
            } else if (key.name === 'return' && key.meta) {
                // In some terminals, Alt+Enter registers as Meta+Return. Treat it the same.
                escapePressed = true;
            } else if (key.name !== 'return') {
                // Reset flag if any other normal key is typed
                escapePressed = false;
            }
        }
    });

    rl.prompt();

    rl.on('line', async (line) => {
        lines.push(line);
        
        // If Escape was pressed just before Enter, execute the submission
        if (escapePressed) {
            const userInput = lines.join('\n').trim();
            lines = []; // Clear buffer for next time
            escapePressed = false; // Reset flag
            
            if (userInput.toLowerCase() === 'exit' || userInput.toLowerCase() === 'quit') {
                rl.close();
                return;
            }
            
            if (userInput) {
                // Temporarily detach raw mode so the agent's internal outputs flow normally
                if (process.stdin.isTTY) process.stdin.setRawMode(false);
                rl.pause();
                
                await runAgentLoop(userInput, MODEL_NAME);
                
                // Reattach after agent finishes
                if (process.stdin.isTTY) process.stdin.setRawMode(true);
                rl.resume();
            }
            
            console.log(); // Blank line for readability
            rl.setPrompt(MAIN_PROMPT);
            rl.prompt();
        } else {
            // Not submitted yet, output a continuation prefix and keep listening
            rl.setPrompt(CONTINUATION_PROMPT);
            rl.prompt();
        }
    }).on('close', () => {
        console.log("\nExiting...");
        process.exit(0);
    });
}

main();
