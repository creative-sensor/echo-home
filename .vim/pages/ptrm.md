### PORT-REMOTE KM[ptrm]

--------

| SSH_HOST     | LOCAL_PORT | REMOTE_IP | REMOTE_PORT | NOTES                                |
| ------------ | ---------- | --------- | ----------- | ------------------------------------ |
| hostA        | 8083       | 127.0.0.1 | 8083        | llm/gemma-4-26B-A4B-it-Q4_K_M        |
| hostB        | 8086       | 127.0.0.1 | 8086        | llm/gemma-4-31B-it-QAT-Q4_0.gguf     |
| hostB        | 8087       | 127.0.0.1 | 8087        | llm/gemma-4-26B-A4B-it-QAT-Q4_0.gguf |
| hostC        | 8088       | 127.0.0.1 | 8088        | llm/gemma-4-12B-it-QAT-Q4_0.gguf     |
| hostC        | 8089       | 127.0.0.1 | 8089        | llm/gemma-4-E4B-it-QAT-Q4_0.gguf     |
| hostD        | 8090       | 127.0.0.1 | 8090        | llm/gemma-4-E2B-it-QAT-Q4_0.gguf     |
| 192.168.1.11 | 8900       | 127.0.0.1 | 8900        | llm/gemma-4-e2b-it-Q8_0.gguf         |

--------

- `ptrmc`: check port status ✅ or ❌
- `ptrmo`: open port via forwarding
