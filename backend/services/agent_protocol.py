"""
agent_protocol.py — one conversation shape, rendered into each provider's.

Agent mode was Claude-only at first for a specific reason: a tool-use
conversation is stateful, half of it is provider-shaped tool_use/tool_result
blocks, and there was no way to hand that history to a different provider
mid-loop. That reason disappears if the loop never holds a provider's shape in
the first place.

So the loop keeps a *normalized* conversation, and this module renders it into
whichever provider serves the next turn and parses the reply back. Rendering is
stateless and total — the entire conversation is rebuilt on every turn — which
is what makes failover mid-conversation safe: a Gemini turn can follow a Claude
turn with no history to translate incrementally and nothing to get out of sync.

Normalized blocks (plain dicts, so they stay JSON-serialisable for the job store):

    {"type": "text",        "text": str}
    {"type": "tool_use",    "id": str, "name": str, "input": dict, "meta": dict}
    {"type": "tool_result", "id": str, "name": str, "content": str}

Messages are `{"role": "user" | "assistant", "content": [block, ...]}`.

`meta` carries provider-native opaque data that has to come back verbatim, and
is the reason a conversation stays on the provider that started it. Gemini 3.x
rejects a replayed functionCall that lost its `thoughtSignature`:

    400 "Function call is missing a thought_signature in functionCall parts.
         This is required for tools to work correctly"

Anthropic has the same rule for thinking blocks. Both are signed, opaque, and
un-reconstructable, so a half-finished conversation genuinely cannot be handed
to a different provider — the first design here assumed it could, and Gemini
said no in production. What normalization still buys is real, just narrower:
the loop holds no provider's shape, so a *new* run picks whichever provider is
up, and adding the next one is a renderer rather than a second loop.

Groq is deliberately absent. It speaks OpenAI-style tool calls and would not be
hard to add, but llama-3.3-70b holding a 40-turn tool loop together is a claim
this has not tested, and a suite silently written by a model that loses the
thread is worse than one provider fewer.
"""

import json
import logging

logger = logging.getLogger(__name__)

TEXT = "text"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"


# ─────────────────────────────────────────────
# Anthropic
# ─────────────────────────────────────────────

def anthropic_tools(schemas: list[dict]) -> list[dict]:
    """Anthropic's tool format is the one the schemas are already written in."""
    return schemas


def to_anthropic(messages: list[dict]) -> list[dict]:
    out = []
    for msg in messages:
        blocks = []
        for b in msg["content"]:
            if b["type"] == TEXT:
                blocks.append({"type": "text", "text": b["text"]})
            elif b["type"] == TOOL_USE:
                blocks.append({
                    "type": "tool_use",
                    "id": b["id"],
                    "name": b["name"],
                    "input": b.get("input") or {},
                })
            elif b["type"] == TOOL_RESULT:
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": b["id"],
                    "content": b["content"],
                })
        out.append({"role": msg["role"], "content": blocks})
    return out


def parse_anthropic(data: dict) -> tuple[list[dict], str]:
    """Reply blocks → normalized. Thinking blocks are dropped deliberately.

    Agent mode asks for no thinking (see `LLMRouter.agent_turn`), because a
    thinking block must be replayed verbatim with its signature and cannot be
    reconstructed from the normalized form — which would pin the whole
    conversation to one provider again, the exact constraint this removes.
    """
    blocks: list[dict] = []
    for b in data.get("content") or []:
        if b.get("type") == "text":
            blocks.append({"type": TEXT, "text": b.get("text", "")})
        elif b.get("type") == "tool_use":
            blocks.append({
                "type": TOOL_USE,
                "id": b.get("id") or "",
                "name": b.get("name") or "",
                "input": b.get("input") or {},
            })
    return blocks, data.get("stop_reason") or ""


# ─────────────────────────────────────────────
# Gemini
# ─────────────────────────────────────────────

def gemini_tools(schemas: list[dict]) -> list[dict]:
    """Anthropic tool schemas → Gemini functionDeclarations.

    Gemini takes an OpenAPI subset, not full JSON Schema. The fields these tools
    use (type/properties/required/enum/description) are all in it, so the
    conversion is a rename — with one real trap: a `parameters` object carrying
    no properties is rejected, so a no-argument tool must omit the key entirely
    rather than send `{"type": "object", "properties": {}}`.
    """
    declarations = []
    for schema in schemas:
        decl = {
            "name": schema["name"],
            "description": schema.get("description", ""),
        }
        params = schema.get("input_schema") or {}
        if params.get("properties"):
            decl["parameters"] = params
        declarations.append(decl)
    return [{"functionDeclarations": declarations}]


def to_gemini(messages: list[dict]) -> list[dict]:
    """Normalized messages → Gemini `contents`.

    Gemini names the assistant "model", and pairs a functionResponse to its call
    by *name* rather than by an id — so the synthetic ids this module assigns on
    parse are for our own bookkeeping and are simply not sent.
    """
    contents = []
    for msg in messages:
        parts = []
        for b in msg["content"]:
            if b["type"] == TEXT:
                parts.append({"text": b["text"]})
            elif b["type"] == TOOL_USE:
                part: dict = {"functionCall": {
                    "name": b["name"],
                    "args": b.get("input") or {},
                }}
                # Replayed verbatim or the next turn is a 400. See the module
                # docstring — Gemini signs its own function calls and requires
                # the signature back on every subsequent request.
                signature = (b.get("meta") or {}).get("thoughtSignature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            elif b["type"] == TOOL_RESULT:
                parts.append({"functionResponse": {
                    "name": b["name"],
                    # Gemini requires an object here, never a bare string.
                    "response": {"result": b["content"]},
                }})
        if parts:
            contents.append({
                "role": "model" if msg["role"] == "assistant" else "user",
                "parts": parts,
            })
    return contents


def parse_gemini(data: dict) -> tuple[list[dict], str]:
    """Gemini candidate parts → normalized blocks.

    Synthesises tool ids (`call-0`, `call-1`, …) because Gemini doesn't issue
    them and the loop pairs results to calls by id. Numbered per reply, which is
    all the pairing needs — a result is always sent in the turn straight after
    the call it answers.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        return [], "empty"

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []

    blocks: list[dict] = []
    for i, part in enumerate(parts):
        # REST returns camelCase; accept the proto spelling too so a field-name
        # change on Google's side degrades to "no signature" rather than a
        # silent drop that only shows up as a 400 on the *next* turn.
        signature = part.get("thoughtSignature") or part.get("thought_signature")
        if "functionCall" in part:
            call = part["functionCall"] or {}
            block = {
                "type": TOOL_USE,
                "id": f"call-{i}",
                "name": call.get("name") or "",
                "input": call.get("args") or {},
            }
            if signature:
                block["meta"] = {"thoughtSignature": signature}
            blocks.append(block)
        elif "text" in part:
            blocks.append({"type": TEXT, "text": part.get("text") or ""})

    # Gemini reports STOP whether it finished talking or finished asking for a
    # tool, so the stop reason is derived from what it actually sent — which is
    # the same rule the loop applies anyway (no tool calls means done).
    stop = "tool_use" if any(b["type"] == TOOL_USE for b in blocks) else "end_turn"
    if candidate.get("finishReason") in ("MAX_TOKENS", "SAFETY", "RECITATION"):
        stop = str(candidate["finishReason"]).lower()
    return blocks, stop


def describe(blocks: list[dict]) -> str:
    """Compact one-line summary of a reply, for logs."""
    bits = []
    for b in blocks:
        if b["type"] == TOOL_USE:
            bits.append(f"{b['name']}({json.dumps(b.get('input') or {})[:60]})")
        elif b["type"] == TEXT and b["text"].strip():
            bits.append(f"text[{len(b['text'])}]")
    return ", ".join(bits) or "(empty)"
