"""Ad-hoc end-to-end smoke test for the chat pipeline.

Connects to the running daemon at ``ws://127.0.0.1:8000/ws/stream``,
sends a ``user_message`` frame, and prints every ``thought`` frame that
shares the generated ``message_id``. Exits once the ``complete`` stage
arrives or after a bounded timeout.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import websockets


async def main(prompt: str) -> int:
    message_id = "msg_" + uuid.uuid4().hex[:12]
    uri = "ws://127.0.0.1:8000/ws/stream"
    deadline = time.monotonic() + 60.0

    async with websockets.connect(uri) as ws:
        hello = json.loads(await ws.recv())
        print(f"[status] {hello['payload']}")

        frame = {
            "type": "user_message",
            "seq": 1,
            "timestamp": "2026-04-19T12:00:00.000Z",
            "payload": {"message_id": message_id, "text": prompt},
        }
        await ws.send(json.dumps(frame))
        print(f"[sent ] message_id={message_id} text={prompt!r}")

        got_complete = False
        seq = 2
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            mtype = msg.get("type")
            # Auto-approve anything the agent asks for so the smoke run
            # doesn't stall on an unhandled approval dialog.
            if mtype == "approval_request":
                p = msg["payload"]
                print(f"[  approve_tool] {p.get('tool_name')} {p.get('tool_args')}")
                await ws.send(json.dumps({
                    "type": "approval_response",
                    "seq": seq,
                    "timestamp": "2026-04-19T12:00:00.000Z",
                    "payload": {"approval_id": p["approval_id"], "approved": True},
                }))
                seq += 1
                continue
            if mtype != "thought":
                continue
            p = msg["payload"]
            if p.get("event_id") != message_id:
                continue
            stage = p.get("stage")
            content = (p.get("content") or "").replace("\n", " ")
            if len(content) > 200:
                content = content[:197] + "…"
            print(f"[{stage:>15s}] {content}")
            if stage == "complete":
                got_complete = True
                break

    print()
    print("✅ complete received" if got_complete else "⚠️  no complete frame")
    return 0 if got_complete else 1


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hi back in one short sentence."
    sys.exit(asyncio.run(main(prompt)))
