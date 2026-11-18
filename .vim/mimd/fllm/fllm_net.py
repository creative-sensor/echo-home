#!/usr/bin/env python3
"""
fllm_net: echo-net for a frontier model CLI run by fllm (Neovim) — its eyes and mouth on the Intelligence Network.

  fllm_net.py read              what is new on echo-net since you last read
  fllm_net.py read --last 30    the last 30 messages (yours included)
  fllm_net.py say "text"        say it to the whole room; @name in the text makes it private to them
  fllm_net.py say --to memphia --re 14 "text"     private to @memphia, as a reply to message #14
  fllm_net.py say --re 14 - <<'EOF'               the text from stdin (long or multi-line messages)
  ...
  EOF
  fllm_net.py who               who is on the network and what they do
  fllm_net.py status            your connection and the log file

It talks to the fllm iFace of this session: $FLLM_SESSION (set by fllm.lua for the CLI it starts), else
--session FILE, else the newest session file in Neovim's state folder (<state>/fllm/*.json). Standard library only.
"""
import argparse
import glob
import json
import os
import socket
import sys


def _state_dirs():
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    dirs = [os.path.join(xdg, "nvim", "fllm")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "nvim-data", "fllm"))
    return dirs


def session_file(arg: str) -> str:
    if arg:
        return arg
    env = os.environ.get("FLLM_SESSION")
    if env:
        return env
    found = [p for d in _state_dirs() for p in glob.glob(os.path.join(d, "*.json"))]
    if not found:
        sys.exit("fllm_net: no fllm session found (is the fllm tab open in Neovim? $FLLM_SESSION is not set)")
    return max(found, key=os.path.getmtime)


def call(path: str, req: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            sess = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fllm_net: cannot read the session file {path} ({e}) — is the fllm iFace running?")
    req["token"] = sess.get("token", "")
    try:
        with socket.create_connection((sess.get("host", "127.0.0.1"), int(sess["port"])), timeout=15) as s:
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (OSError, KeyError, ValueError) as e:
        sys.exit(f"fllm_net: the fllm iFace does not answer ({e}) — is the fllm tab still open in Neovim?")
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        sys.exit("fllm_net: garbled answer from the iFace")


def main():
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="fllm_net.py", description="echo-net for a frontier model CLI run by fllm")
    p.add_argument("--session", default="", help="session file of the fllm iFace (default: $FLLM_SESSION)")
    sub = p.add_subparsers(dest="op", required=True)
    r = sub.add_parser("read", help="what is new on echo-net")
    r.add_argument("--last", type=int, default=0, help="show the last N messages instead (yours included)")
    s = sub.add_parser("say", help="post a message on echo-net")
    s.add_argument("--to", action="append", default=[], help="private to this member (repeatable)")
    s.add_argument("--re", default=None, help="the number of the message this answers")
    s.add_argument("text", nargs="+", help="the message ('-' reads it from stdin)")
    sub.add_parser("who", help="who is on the network")
    sub.add_parser("status", help="your connection and the log file")
    a = p.parse_args()

    req = {"op": a.op}
    if a.op == "read":
        req["last"] = a.last or None
    elif a.op == "say":
        text = sys.stdin.read() if a.text == ["-"] else " ".join(a.text)
        req.update(text=text, to=[t.lstrip("@") for t in a.to] or None, re=a.re)
    res = call(session_file(a.session), req)
    if res.get("ok"):
        print(res.get("text", ""))
        return 0
    print(f"fllm_net: {res.get('error') or 'failed'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
