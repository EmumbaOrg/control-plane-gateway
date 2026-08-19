#!/usr/bin/env python3
"""
Decompress captured exchanges into plain, double-clickable files.

    python3 export_captures.py            # reads ./capture, writes ./export

For each exchange it writes, per session folder:
    <stamp>.request.json          pretty-printed request Claude Code sent
    <stamp>.response.events.txt   the raw SSE stream, one event per block
    <stamp>.response.text.txt     just the assistant's reconstructed text
    <stamp>.summary.txt           model, tokens, cache, stop reason, timing

Plus export/index.csv — one row per exchange for a spreadsheet view.
"""

import csv
import gzip
import json
from collections import Counter
from pathlib import Path

CAP = Path("./capture")
OUT = Path("./export")


def read_gz_text(p: Path) -> str:
    with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
        return f.read()


def decode_response(sse: str):
    text, usage, model, stop, events = "", {}, None, None, []
    cur_event = None
    for line in sse.splitlines():
        line = line.strip()
        if line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
            events.append(cur_event)
            continue
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        t = ev.get("type")
        if t == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
            text += ev["delta"]["text"]
        if t == "message_start":
            msg = ev.get("message", {})
            model = msg.get("model")
            usage = dict(msg.get("usage", {}))
        if t == "message_delta":
            if ev.get("usage"):
                usage.update(ev["usage"])
            stop = ev.get("delta", {}).get("stop_reason")
    return text, usage, model, stop, events


def summarise_request(req: dict) -> str:
    lines = []
    lines.append(f"model      : {req.get('model')}")
    lines.append(f"max_tokens : {req.get('max_tokens')}")
    lines.append(f"stream     : {req.get('stream')}")
    lines.append(f"top keys   : {', '.join(req.keys())}")
    s = req.get("system")
    if isinstance(s, list):
        lines.append(f"system     : {len(s)} block(s)")
        for i, b in enumerate(s):
            t = b.get("text", "") if isinstance(b, dict) else str(b)
            cc = "  [cache_control]" if isinstance(b, dict) and b.get("cache_control") else ""
            lines.append(f"   [{i}] {len(t)} chars{cc}")
    lines.append(f"tools      : {len(req.get('tools') or [])}")
    lines.append(f"messages   : {len(req.get('messages', []))} turn(s)")
    for m in req.get("messages", []):
        c = m["content"]
        if isinstance(c, str):
            lines.append(f"   {m['role']}: {len(c)} chars")
        else:
            for b in c:
                lines.append(f"   {m['role']}: [{b.get('type')}]")
    return "\n".join(lines)


def main():
    if not CAP.exists():
        print(f"No capture dir at {CAP.resolve()}")
        return
    OUT.mkdir(exist_ok=True)
    rows = []

    for req_path in sorted(CAP.glob("*/*.request.json.gz")):
        session = req_path.parent.name
        stamp = req_path.name.replace(".request.json.gz", "")
        resp_path = req_path.with_name(f"{stamp}.response.sse.gz")

        out_dir = OUT / session
        out_dir.mkdir(parents=True, exist_ok=True)

        # request
        try:
            req = json.loads(read_gz_text(req_path))
            (out_dir / f"{stamp}.request.json").write_text(
                json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            req = {}
            (out_dir / f"{stamp}.request.RAW.txt").write_text(
                read_gz_text(req_path), encoding="utf-8")
            print(f"  ! {stamp} request not JSON ({e}); wrote raw")

        # response
        text = model = stop = ""
        usage, events = {}, []
        if resp_path.exists():
            sse = read_gz_text(resp_path)
            (out_dir / f"{stamp}.response.events.txt").write_text(sse, encoding="utf-8")
            text, usage, model, stop, events = decode_response(sse)
            (out_dir / f"{stamp}.response.text.txt").write_text(text, encoding="utf-8")

        # summary
        summary = (
            f"EXCHANGE {stamp}\nsession {session}\n\n"
            f"--- REQUEST ---\n{summarise_request(req)}\n\n"
            f"--- RESPONSE ---\n"
            f"model       : {model}\n"
            f"stop_reason : {stop}\n"
            f"assistant   : {text!r}\n"
            f"input_tokens              : {usage.get('input_tokens')}\n"
            f"cache_read_input_tokens   : {usage.get('cache_read_input_tokens')}\n"
            f"cache_creation_input_tokens: {usage.get('cache_creation_input_tokens')}\n"
            f"output_tokens             : {usage.get('output_tokens')}\n"
            f"SSE events  : {dict(Counter(events))}\n"
        )
        (out_dir / f"{stamp}.summary.txt").write_text(summary, encoding="utf-8")

        rows.append({
            "session": session, "stamp": stamp, "model": model,
            "assistant_text": text.strip()[:60],
            "input_tokens": usage.get("input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": stop,
            "request_bytes": req_path.stat().st_size,
        })

    if rows:
        with (OUT / "index.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"Exported {len(rows)} exchange(s) to {OUT.resolve()}")


if __name__ == "__main__":
    main()
