# llamark : prompt memory
--------------------------------
### 0 DESCRIPTION


### 1 SOLUTION


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

```

Cuộc trò chuyện mới
Đăng nhập để lưu hoạt động
Trò chuyện với Gemini

how to save cache --slot-save-path llama cpp

To save and persist the KV cache to disk using llama.cpp's server (llama-server), you use the --slot-save-path flag. This enables persistent slot storage and activates the API endpoints needed to save and restore states.  
1. Starting the Server

Launch llama-server by specifying a directory path where the slot cache files will be written:
Bash

llama-server -m ./your-model.gguf --slot-save-path ./cache_dir

    --slot-save-path PATH: Specifies the directory (e.g., ./cache_dir) where slot data and KV caches are stored on disk.

    Note: This option also automatically enables the /slots API endpoints.

2. Saving and Restoring via API

Once the server is running with the save path enabled, you can trigger cache saves and restores manually or programmatically via HTTP POST requests to the /slots endpoint.

    Save a slot's cache:
    Bash

    curl -X POST "http://localhost:8080/slots/1?action=save"

    (Replace 1 with your target slot ID).

    Restore a slot's cache:
    Bash

    curl -X POST "http://localhost:8080/slots/1?action=restore"

Alternative: Automatic Caching in Memory

If your goal is to prevent prompt re-processing across sequential requests during a single server session without necessarily dumping files permanently to your hard drive, ensure your server is using its built-in idle slot caching (enabled by default):
Bash

llama-server -m ./your-model.gguf --cache-ram 8192 --cache-idle-slots

    --cache-ram: Allocates memory pool size in MiB for caching prompt states.

    --cache-idle-slots: Automatically preserves idle slots in the prompt cache when new tasks come in.

how to find slot id

To find the active slot IDs on your running llama.cpp server, you can query the /slots endpoint.
How to Check Slot Status

Run the following curl command in your terminal to view the status, ID, and prompt state of all available slots:
Bash

curl http://localhost:8080/slots

(Make sure to update http://localhost:8080 if your server is running on a different port or host).
What to Look For

The server will return a JSON array containing details for each slot. A typical response looks like this:
JSON

[
  {
    "id": 0,
    "state": 1,
    "is_processing": false,
    "n_prompt_tokens": 128,
    "n_past": 128
  }
]

    "id": 0: This is the slot ID you need to use in your save/restore commands (e.g., /slots/0?action=save).

    "state": Indicates whether the slot is idle, processing, or has a saved cache loaded.

How do I save a specific slot cache using the API?

To save a specific slot's cache using the llama-server API, send a POST request to the slot endpoint with the action=save parameter.
1. Send the Save Request

Run the following curl command, replacing 0 with your target slot ID:
Bash

curl -X POST "http://localhost:8080/slots/0?action=save"

If successful, the server will trigger a save operation for that slot's current KV cache state into the directory you configured with --slot-save-path.
2. Verify the Save Was Successful

    Check the server logs: Look at the terminal running llama-server. It should output a log confirming that the slot state was saved (e.g., writing data to the cache directory).

    Check the filesystem: Navigate to the folder you specified in --slot-save-path (e.g., ./cache_dir) and verify that a file corresponding to that slot's identifier has been created or updated.

 curl -X POST "http://localhost:8090/slots/3?action=save"

{"error":{"code":500,"message":"[json.exception.parse_error.101] parse error at line 1, column 1: attempting to parse an empty input; check that your input string or stream contains the expected JSON","type":"server_error"}}

This error occurs because the llama.cpp server expects a JSON payload containing a filename parameter for the save action, but received an empty body.  

To fix this, include the Content-Type header and provide a JSON body with the target filename:  
Bash

curl -X POST "http://localhost:8090/slots/3?action=save" \
  -H "Content-Type: application/json" \
  -d '{"filename": "slot3_cache.bin"}'

    filename: The file will be saved inside the directory you specified earlier via the --slot-save-path flag when starting llama-server.

should we treat each slot id the same chat session

No, you should not tie a chat session strictly to a permanent slot ID (like slot 3).

Slot IDs are dynamic server-execution resources, not permanent identities for your chat conversations. How they work dictates a different approach:
Why You Shouldn't Treat Slot IDs as Chat Sessions

    Slots are recycled: When multiple users or parallel requests hit llama-server, the server assigns whichever slot happens to be free (0, 1, 2, 3, etc.) to process the request.

    Slots overwrite data: If Chat Session A is using Slot 3, and a new request comes in that gets assigned to Slot 3, the context in Slot 3 will be overwritten or appended to, destroying your previous session's cache unless it was explicitly saved.

    Dynamic allocation: If you run llama-server with -np 4 (4 parallel slots), slots are ephemeral workers.

The Correct Pattern: Slot ID vs. Session File

Instead of locking a chat session to a slot ID, you should map your chat sessions to unique filename states, and load them into whichever slot is currently available.  

    Assign a unique Session ID to every user chat (e.g., a UUID like user_xyz_chat_123).  

    Map that to a distinct cache file name on your disk (e.g., session_123.bin).  

    When a message arrives for Chat 123:

        Find any available slot (e.g., check /slots to find an idle one, or just use Slot 0 if you run -np 1).

        Restore your session's file into that slot using the API:  
        Bash

        curl -X POST "http://localhost:8090/slots/0?action=restore" \
          -H "Content-Type: application/json" \
          -d '{"filename": "session_123.bin"}'

        Run your inference/chat completion.

        Save the updated state back to that session's file:
        Bash

        curl -X POST "http://localhost:8090/slots/0?action=save" \
          -H "Content-Type: application/json" \
          -d '{"filename": "session_123.bin"}'

By decoupling the chat session file from the hardware slot ID, multiple users or sequential chats can seamlessly share the same server infrastructure without corrupting each other's history.

how to get cache hit if new prompt is put

To get a cache hit when sending a new prompt to llama.cpp, the server relies on matching the prefix (the beginning) of your new prompt against what is already stored in a slot's KV cache.  

When you put a new prompt, llama.cpp handles cache hits in two ways:
1. In-Memory Prefix Caching (Automatic)

By default, llama-server has built-in prompt caching (--cache-prompt). If a slot already contains a conversation history, and your new request shares the exact same starting text (like the system prompt or previous chat turns), the server will automatically reuse the cached tokens and only process the new trailing tokens.  

    The Rule: The prefix must match character-for-character, token-for-token. Even a single hidden character, timestamp, or extra space at the beginning will break the cache and cause a full re-evaluation.  

    How to keep it active: Keep your system prompts, developer messages, and initial context strictly identical across requests. Append new user/assistant turns strictly to the end.

2. Disk-Restored Caching (Manual / Programmatic)

If your server restarted or the slot was cleared, an in-memory cache won't exist. To force a cache hit using your saved files from --slot-save-path, you must explicitly restore the cache file before sending the new prompt:  

    Restore the saved session file into an available slot:
    Bash

    curl -X POST "http://localhost:8090/slots/3?action=restore" \
      H "Content-Type: application/json" \
      -d '{"filename": "session_123.bin"}'

    Send your new prompt to the chat completions endpoint. Because the slot now holds the exact KV state of your previous conversation, llama.cpp will instantly recognize the matching prefix and give you a cache hit, processing only the newly appended text.

if chat session fall on a different slot while we have multiple cache file on disk, how we lookup to enable cache hit

Because llama.cpp slot IDs are ephemeral execution lanes rather than fixed containers for a specific conversation, a chat session can safely land on any available slot. To achieve a cache hit when this happens, your application layer must explicitly bind the user's session to its respective cache file on disk, regardless of which slot is currently free.
How to Route and Restore Cache Files Across Different Slots
1
Map Session IDs to Filenames
Application Layer

Keep a lookup dictionary or database in your application code mapping each unique chat session ID to its corresponding disk filename (e.g., session_abc123.bin).
2
Query Available Slots
API Check

Send a GET request to /slots to inspect the server and find an idle slot (e.g., id: 2).

    Verification: Confirm the slot's "state" field indicates it is idle and ready for work.

3
Restore the Correct Cache File
API Action

Before passing the new user prompt, send a POST request to load that session's specific disk file into whichever slot was assigned, using the /slots/{id}?action=restore endpoint:
Bash

curl -X POST "http://localhost:8090/slots/2?action=restore" \
  H "Content-Type: application/json" \
  -d '{"filename": "session_abc123.bin"}'

    Verification: Check the server logs to ensure it reports a successful cache/context restoration for that slot instead of a full re-prefill.

4
Run Inference and Save State
Completion

Send your chat completion request. Once finished, immediately overwrite or update the cache file by saving the slot state back to the same filename:
Bash

curl -X POST "http://localhost:8090/slots/2?action=save" \
  -H "Content-Type: application/json" \
  -d '{"filename": "session_abc123.bin"}'

is bin file not bound to specific slot id? subsequently we can load into another slot id

No, the .bin cache file is not permanently bound to a specific slot ID.

A .bin file simply contains the serialized KV cache (and any recurrent state) for a specific conversation. You can save a session from Slot 3 into a file (e.g., session_123.bin) and later restore that exact same file into Slot 0, Slot 1, or any other available slot on the server.  
The Only Rules for Cross-Slot Loading:

    The model must match: You cannot load a cache file generated by one model into a server running a completely different model architecture or size.

    Context size (-c) must be compatible: The target server slot must have a context window large enough to accommodate the saved state.

This flexibility is precisely what allows you to handle multiple users or concurrent sessions dynamically: your application tracks the .bin file associated with a user's chat ID, finds any free slot on the server, and injects that file via the restore endpoint.

is bin file able to append new data?

No, you cannot directly append raw data to a .bin cache file on disk using standard file tools. The file is a complex binary snapshot of the model's internal KV cache and recurrent state structures, not a simple text log.

However, you can update the file with new conversation data dynamically through the llama.cpp API.
How to "Append" New Data to a Session File

Instead of writing to the file directly, you update the file by letting the server process new turns and then saving a fresh snapshot over the old filename:

    Restore the existing .bin file into a slot:
    Bash

    curl -X POST "http://localhost:8090/slots/0?action=restore" \
      -H "Content-Type: application/json" \
      -d '{"filename": "session_123.bin"}'

    Send a new message/prompt via your normal chat completion request. The model will process the new tokens, building upon the restored cache.

    Save the slot state back to the exact same filename:
    Bash

    curl -X POST "http://localhost:8090/slots/0?action=save" \
      -H "Content-Type: application/json" \
      -d '{"filename": "session_123.bin"}'

This action overwrites session_123.bin on disk with a new snapshot that includes all the previous history plus the newly added conversation turns.



 if we restore 1 slot, will cache data be shared to the rest

No. When you restore a cache file into a specific slot, that data is loaded exclusively into that individual slot's memory buffer. It is not automatically shared or broadcasted to any other parallel slots.
Key Details on Slot Isolation

    Independent Workspaces: If you run llama-server with multiple slots (e.g., -np 4) and restore a session file into Slot 0, Slot 1, Slot 2, and Slot 3 remain completely unaware of it.

    Manual Routing Required: If a subsequent request for that same user happens to land on a different slot due to server scheduling, you must explicitly call the restore endpoint for that newly assigned slot using the user's .bin file.

How to Share Common Data Across All Slots Automatically

If your goal is to share a common prefix (such as a massive system prompt or shared set of rules) across all slots simultaneously without manually restoring files for every request, you should rely on llama.cpp's built-in prefix optimization features instead of manual slot file restoration:

    --cache-reuse: Automatically detects matching token sequences across different slots and reuses the computed KV cache.

    --system-prompt-file: Loads a static prompt into memory so all parallel slots can reference it as a shared base prefix.  

that mean duplicate of cache data in VRAM for each slot restored

Yes, exactly. If you have multiple slots and restore different chat sessions into them simultaneously, each slot maintains its own isolated KV cache memory buffer.

However, there is an important nuance regarding how llama.cpp handles memory optimization:
1. KV Cache Allocation (-np)

When you start llama-server with -np 4, the server typically allocates the total available cache size and slices it across the slots, or allocates independent buffers per slot depending on your flags. Each active slot holds its own state copy. If two slots have overlapping conversation histories (e.g., they share the same system prompt), llama.cpp's internal memory management handles prefix caching efficiently, but distinct session branches consume separate memory.
2. The Single-User Advantage

Because you are using a single-user setup (where -np 1), you completely bypass this duplication issue. Since there is only one slot (slot 0), there is only ever one active cache loaded into memory at any given time, maximizing your VRAM efficiency for that specific user's context window.


```

--------------------------------
```json
{ "project_code": "MEMR" , "links": "" , "location": "" , "fpoint": "" }
```
