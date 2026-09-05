"""仅用于 hermetic 测试的 Claude CLI wire 服务端，不调用模型或工具。"""

import json
import os
import sys

MODE = sys.argv[1]
SESSION = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith(("--session-id=", "--resume=")))
MODEL = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--model=")), "model-a")
active = False
initialized = False


def send(message):
    print(json.dumps(message), flush=True)


def control(request, data):
    send({"type": "control_response", "response": {
        "subtype": "success", "request_id": request["request_id"], "response": data,
    }})


def result(success):
    send({"type": "result", "session_id": SESSION, "is_error": not success,
          "subtype": "success" if success else "error_during_execution",
          "result": "streamed 中文" if success else "", "errors": [] if success else ["interrupted"]})


for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("type") == "control_request":
        subtype = message["request"]["subtype"]
        if subtype == "initialize":
            if MODE == "bad-control":
                send({"type": "control_response", "response": {
                    "request_id": message["request_id"], "subtype": "unexpected",
                }})
            elif MODE == "init-eof":
                sys.exit(0)
            else:
                control(message, {"models": [
                    {"value": "model-a", "displayName": "A"},
                    {"value": "model-b", "displayName": "B"},
                ]})
        elif subtype == "interrupt":
            control(message, {})
            if active:
                result(False)
                active = False
        else:
            raise RuntimeError("unknown control request")
    elif message.get("type") == "user":
        active = True
        if not initialized:
            initialized = True
            send({"type": "system", "subtype": "init",
                  "session_id": SESSION if MODE != "wrong-session" else "unrelated",
                  "model": MODEL if MODE != "model-refused" else "wrong-model",
                  "claude_code_version": "fixture-1", "argv": sys.argv[2:],
                  "parent_env": {key: os.environ.get(key) for key in (
                      "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_SESSION_ID")}})
        send({"type": "future_event", "session_id": SESSION, "nested": {"retained": [1, 2]}})
        send({"type": "stream_event", "session_id": SESSION, "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": "streamed 中文"},
        }})
        if MODE == "eof":
            sys.exit(0)
        send({"type": "control_request", "request_id": "approval-1", "request": {
            "subtype": "can_use_tool", "tool_name": "Write", "input": {"file_path": "never-write"},
        }})
    elif message.get("type") == "control_response":
        response = message["response"]
        if response["response"]["behavior"] != "deny":
            raise RuntimeError("permission was not denied")
        if MODE != "hold":
            result(True)
            active = False
    else:
        raise RuntimeError("unknown input")
