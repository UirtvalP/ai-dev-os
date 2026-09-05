"""仅用于 hermetic 测试的 ACP 服务端；不调用真实 Cursor、工具或网络。"""

import json
import os
import sys

MODE = sys.argv[1]
SESSION = "cursor-session"
active = None
current_model = "model-a"


def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def response(request, result):
    send({"id": request["id"], "result": result})


def config():
    if MODE == "no-models":
        return {"configOptions": []}
    return {"configOptions": [{
        "id": "model", "name": "Model", "category": "model", "type": "select",
        "currentValue": current_model,
        "options": [{"value": "model-a", "name": "A"}, {"value": "model-b", "name": "B"}],
    }]}


def update(kind, **fields):
    send({"method": "session/update", "params": {
        "sessionId": SESSION, "update": {"sessionUpdate": kind, **fields},
    }})


for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    params = message.get("params", {})
    if method == "initialize":
        if MODE == "init-eof":
            sys.exit(0)
        if MODE == "init-timeout":
            continue
        response(message, {
            "protocolVersion": 99 if MODE == "bad-version" else 1,
            "agentInfo": {"name": "hermetic-cursor", "version": "fixture-1"},
            "agentCapabilities": {"loadSession": MODE != "no-resume"},
            "authMethods": [{"id": "cursor_login"}],
        })
    elif method == "authenticate":
        if params != {"methodId": "cursor_login"}:
            raise RuntimeError("unexpected authentication")
        response(message, {})
    elif method == "session/new":
        response(message, {"sessionId": SESSION, **config()})
    elif method == "session/load":
        SESSION = params["sessionId"]
        if MODE == "resume-error":
            send({"id": message["id"], "error": {"code": -32000, "message": "provider problem"}})
        else:
            update("user_message_chunk", content={"type": "text", "text": "historic user"})
            update("agent_message_chunk", content={"type": "text", "text": "historic reply"})
            response(message, None)
    elif method == "session/set_mode":
        if params["modeId"] != "ask":
            raise RuntimeError("write mode forbidden")
        response(message, {})
    elif method == "session/set_config_option":
        current_model = params["value"] if MODE != "model-refused" else "model-a"
        response(message, config())
    elif method == "session/prompt":
        active = message
        update("future_chunk", future={"unchanged": [1, {"nested": True}]},
               parent_env={key: os.environ.get(key) for key in (
                   "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_SESSION_ID")},
               received=message)
        update("agent_message_chunk", content={"type": "text", "text": "streamed 中文"})
        update("tool_call", toolCallId="tool-1", title="Read", status="pending")
        if MODE == "eof":
            sys.exit(0)
        if MODE == "wrong-session":
            SESSION = "other-session"
            update("agent_message_chunk", content={"type": "text", "text": "wrong"})
        send({"id": "permission-1", "method": "session/request_permission", "params": {
            "sessionId": SESSION, "toolCall": {"toolCallId": "tool-1"}, "options": [
                {"optionId": "grant", "kind": "allow_once"},
                {"optionId": "reject", "kind": "reject_once"},
            ],
        }})
    elif method == "session/cancel" and active:
        response(active, {"stopReason": "cancelled"})
        active = None
    elif message.get("id") == "permission-1":
        if message["result"] != {"outcome": {"outcome": "selected", "optionId": "reject"}}:
            raise RuntimeError("permission was not denied")
        update("tool_call_update", toolCallId="tool-1", status="failed")
        if MODE != "hold" and active:
            response(active, {"stopReason": "end_turn" if MODE != "bad-stop" else "made-up"})
            active = None
    else:
        if "id" in message:
            send({"id": message["id"], "error": {"code": -32601, "message": "unknown method"}})
