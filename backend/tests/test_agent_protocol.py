"""test_agent_protocol.py — one conversation, rendered into each provider's shape.

These tests exist because the rendering is what makes mid-conversation failover
safe. If a normalized conversation doesn't round-trip through a provider, a
rate limit on one provider silently corrupts the history rather than falling
back cleanly — and the failure surfaces many turns later as nonsense output.
"""

import pytest

from services import agent_protocol as ap
from services.agent_tools import TOOL_SCHEMAS


CONVO = [
    {"role": "user", "content": [{"type": ap.TEXT, "text": "write tests"}]},
    {"role": "assistant", "content": [
        {"type": ap.TOOL_USE, "id": "c1", "name": "find_anchors", "input": {"kind": "testid"}},
    ]},
    {"role": "user", "content": [
        {"type": ap.TOOL_RESULT, "id": "c1", "name": "find_anchors", "content": "2 anchors"},
    ]},
]


class TestAnthropicRendering:
    def test_tool_result_carries_the_id_anthropic_pairs_on(self):
        out = ap.to_anthropic(CONVO)
        assert out[2]["content"][0]["tool_use_id"] == "c1"

    def test_roles_are_unchanged(self):
        assert [m["role"] for m in ap.to_anthropic(CONVO)] == \
            ["user", "assistant", "user"]

    def test_parses_text_and_tool_use(self):
        blocks, stop = ap.parse_anthropic({
            "content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "x", "name": "read_file", "input": {"path": "a"}},
            ],
            "stop_reason": "tool_use",
        })
        assert stop == "tool_use"
        assert [b["type"] for b in blocks] == [ap.TEXT, ap.TOOL_USE]
        assert blocks[1]["input"] == {"path": "a"}

    def test_thinking_blocks_are_dropped(self):
        """They can't be rebuilt from the normalized form, so carrying them would
        pin the run to one provider — the exact thing this module removes."""
        blocks, _ = ap.parse_anthropic({
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "text", "text": "done"},
            ],
            "stop_reason": "end_turn",
        })
        assert [b["type"] for b in blocks] == [ap.TEXT]


class TestGeminiRendering:
    def test_the_assistant_is_called_model(self):
        assert [c["role"] for c in ap.to_gemini(CONVO)] == ["user", "model", "user"]

    def test_a_tool_call_becomes_a_function_call(self):
        parts = ap.to_gemini(CONVO)[1]["parts"]
        assert parts[0]["functionCall"] == {
            "name": "find_anchors", "args": {"kind": "testid"}}

    def test_a_tool_result_pairs_by_name_and_wraps_in_an_object(self):
        """Gemini issues no call ids, so it pairs on name — and rejects a bare
        string where it wants an object."""
        parts = ap.to_gemini(CONVO)[2]["parts"]
        assert parts[0]["functionResponse"]["name"] == "find_anchors"
        assert parts[0]["functionResponse"]["response"] == {"result": "2 anchors"}

    def test_parses_a_function_call_and_synthesises_an_id(self):
        blocks, stop = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "write_file", "args": {"filename": "a.ts"}}},
        ]}, "finishReason": "STOP"}]})
        assert stop == "tool_use"
        assert blocks[0]["id"]                       # the loop pairs on this
        assert blocks[0]["name"] == "write_file"

    def test_a_thought_signature_is_captured_and_replayed_verbatim(self):
        """Gemini 3.x signs its own function calls and 400s a replay that lost
        the signature: "Function call is missing a thought_signature". This is
        the single reason an agent conversation is pinned to one provider."""
        blocks, _ = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "read_file", "args": {}},
             "thoughtSignature": "SIG-123"},
        ]}}]})
        assert blocks[0]["meta"]["thoughtSignature"] == "SIG-123"

        replayed = ap.to_gemini([{"role": "assistant", "content": blocks}])
        assert replayed[0]["parts"][0]["thoughtSignature"] == "SIG-123"

    def test_the_proto_spelling_is_accepted_too(self):
        """A field-name change on Google's side should degrade to 'no signature',
        not vanish silently and 400 on the *next* turn."""
        blocks, _ = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "x", "args": {}}, "thought_signature": "S"},
        ]}}]})
        assert blocks[0]["meta"]["thoughtSignature"] == "S"

    def test_a_call_with_no_signature_sends_no_signature_key(self):
        blocks, _ = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "x", "args": {}}}]}}]})
        part = ap.to_gemini([{"role": "assistant", "content": blocks}])[0]["parts"][0]
        assert "thoughtSignature" not in part

    def test_a_plain_text_reply_ends_the_turn(self):
        """Gemini says STOP either way, so 'done' is derived from what it sent."""
        blocks, stop = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"text": "All finished."}]}, "finishReason": "STOP"}]})
        assert stop == "end_turn"
        assert blocks[0]["text"] == "All finished."

    def test_a_truncated_reply_is_not_mistaken_for_a_finished_one(self):
        blocks, stop = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"text": "half"}]}, "finishReason": "MAX_TOKENS"}]})
        assert stop == "max_tokens"

    def test_an_empty_response_is_reported_not_crashed(self):
        blocks, stop = ap.parse_gemini({"candidates": []})
        assert (blocks, stop) == ([], "empty")

    def test_a_missing_parts_list_is_survivable(self):
        blocks, _ = ap.parse_gemini({"candidates": [{"content": {}}]})
        assert blocks == []


class TestGeminiToolSchemas:
    def test_schemas_convert_to_function_declarations(self):
        decls = ap.gemini_tools(TOOL_SCHEMAS)[0]["functionDeclarations"]
        assert {d["name"] for d in decls} == {s["name"] for s in TOOL_SCHEMAS}
        assert all(d["description"] for d in decls)

    def test_a_no_argument_tool_omits_parameters_entirely(self):
        """Gemini rejects a parameters object with no properties — sending
        `{"type":"object","properties":{}}` 400s the whole call."""
        decls = ap.gemini_tools(TOOL_SCHEMAS)[0]["functionDeclarations"]
        by_name = {d["name"]: d for d in decls}
        assert "parameters" not in by_name["list_files"]
        assert "parameters" not in by_name["run_test"]

    def test_a_tool_with_arguments_keeps_its_schema(self):
        decls = ap.gemini_tools(TOOL_SCHEMAS)[0]["functionDeclarations"]
        write = next(d for d in decls if d["name"] == "write_file")
        assert set(write["parameters"]["required"]) == {
            "filename", "description", "content"}


class TestRoundTrip:
    """The property that actually matters: what one provider says can be replayed
    to the other. This is failover in one assertion."""

    @pytest.mark.parametrize("render", [ap.to_anthropic, ap.to_gemini])
    def test_a_gemini_reply_can_be_replayed_to_either_provider(self, render):
        blocks, _ = ap.parse_gemini({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "read_file", "args": {"path": "a.tsx"}}}]}}]})
        convo = [
            {"role": "user", "content": [{"type": ap.TEXT, "text": "go"}]},
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [{
                "type": ap.TOOL_RESULT, "id": blocks[0]["id"],
                "name": "read_file", "content": "<div/>"}]},
        ]
        assert len(render(convo)) == 3          # renders without losing a turn

    @pytest.mark.parametrize("render", [ap.to_anthropic, ap.to_gemini])
    def test_an_anthropic_reply_can_be_replayed_to_either_provider(self, render):
        blocks, _ = ap.parse_anthropic({"content": [
            {"type": "tool_use", "id": "t9", "name": "read_file", "input": {"path": "b"}}],
            "stop_reason": "tool_use"})
        convo = [
            {"role": "assistant", "content": blocks},
            {"role": "user", "content": [{
                "type": ap.TOOL_RESULT, "id": "t9", "name": "read_file", "content": "x"}]},
        ]
        assert len(render(convo)) == 2
