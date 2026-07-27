### PORT-REMOTE KM[ptrm]

--------

| SSH_HOST     | LOCAL_PORT | REMOTE_IP | REMOTE_PORT | USAGE                                |
| ------------ | ---------- | --------- | ----------- | ------------------------------------ |
| hostA        | 8082       | 127.0.0.1 | 8082        | llm/gemma-4-26B-A4B-it-QAT-Q4_0.gguf |
| hostB        | 8087       | 127.0.0.1 | 8087        | web-ui                               |
| hostC        | 8080       | 127.0.0.1 | 8090        | llm/gemma-4-E2B-it-QAT-Q4_0.gguf     |
| 192.168.1.11 | 8900       | 127.0.0.1 | 8900        | llm/gemma-4-e2b-it-Q8_0.gguf         |

--------

- `ptrmc`: check port status ✅ or ❌
- `ptrmo`: open port via forwarding
