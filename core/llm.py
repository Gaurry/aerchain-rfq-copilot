"""Minimal tool-use loop shared by the RFQ co-pilot and the analyst.

The model picks tools; our Python runs them; results go back; repeat until the
model answers in text. Every call is logged so the UI can show the working."""
from __future__ import annotations

import json
from typing import Callable

import anthropic


def run_tool_loop(client: anthropic.Anthropic, model: str, system: list[dict] | str, messages: list[dict],
                  tools: list[dict], dispatch: Callable[[str, dict], object], max_turns: int = 10,
                  max_tokens: int = 4000) -> tuple[str, list[dict]]:
    """Mutates `messages` (keeps full context for the next turn). Returns (final_text, calls)."""
    calls: list[dict] = []
    for _ in range(max_turns):
        msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                     tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in msg.content]})
        uses = [b for b in msg.content if b.type == "tool_use"]
        if not uses:
            return "".join(b.text for b in msg.content if b.type == "text").strip(), calls
        results = []
        for u in uses:
            try:
                out = dispatch(u.name, dict(u.input))
                ok = True
            except Exception as e:  # tool errors go back to the model, not to the user as a crash
                out, ok = {"error": str(e)}, False
            calls.append({"tool": u.name, "input": dict(u.input), "ok": ok})
            results.append({"type": "tool_result", "tool_use_id": u.id,
                            "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str),
                            **({} if ok else {"is_error": True})})
        messages.append({"role": "user", "content": results})
    return "I couldn't finish that in a reasonable number of steps. Could you narrow the question?", calls
