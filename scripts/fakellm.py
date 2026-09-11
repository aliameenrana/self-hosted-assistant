"""Stand-in for llama-server. Scripted responses, OpenAI-shaped.

Exists so the harness can be verified without weights, and so the fabrication
test can force a model that tries to lie about tool use.
"""
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
MODE = os.environ.get("FAKE_MODE", "tool")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    has_tools = "tools" in body
    messages = body["messages"]
    already_ran = any(m.get("role") == "tool" for m in messages)

    if has_tools and not already_ran and MODE in ("tool", "liar"):
        return {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "calculator",
                    "arguments": json.dumps({"expression": "17*23"})}}]}}]}
    if has_tools:
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    text = ("I ran a web search and found seventeen sources confirming it."
            if MODE == "liar" else "391. That is 17 times 23.")

    def stream():
        for word in text.split():
            yield "data: " + json.dumps({"choices": [{"delta": {"content": word + " "}}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
