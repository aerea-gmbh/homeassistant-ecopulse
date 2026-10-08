"""The SSE framing of ADR-230 §6, parsed without a network."""

from __future__ import annotations

import pytest

from custom_components.ecopulse.sse import SseEvent, SseParser, SseProtocolError


def test_parses_the_wire_framing_of_the_adr() -> None:
    parser = SseParser()

    events = parser.feed(
        b"retry: 5000\n\n"
        b'event: snapshot\ndata: {"catalogRevision":"abc","devices":[],"values":[]}\n\n'
        b": ping\n\n"
        b'event: values\ndata: {"values":[{"id":"x","value":1,"ts":null}]}\n\n'
    )

    assert events == [
        SseEvent("snapshot", {"catalogRevision": "abc", "devices": [], "values": []}),
        SseEvent("values", {"values": [{"id": "x", "value": 1, "ts": None}]}),
    ]


def test_an_event_split_across_chunks_is_emitted_once_complete() -> None:
    parser = SseParser()

    assert parser.feed(b"event: catalog\nda") == []
    assert parser.feed(b'ta: {"catalogRevision":"r2"}\n') == []
    assert parser.feed(b"\n") == [SseEvent("catalog", {"catalogRevision": "r2"})]


def test_crlf_line_endings_are_accepted() -> None:
    parser = SseParser()

    events = parser.feed(b'event: catalog\r\ndata: {"catalogRevision":"r"}\r\n\r\n')

    assert events == [SseEvent("catalog", {"catalogRevision": "r"})]


def test_a_ping_and_retry_produce_no_event() -> None:
    parser = SseParser()

    assert parser.feed(b"retry: 5000\n\n: ping\n\n: ping\n\n") == []


def test_unknown_fields_are_ignored_per_sse_spec() -> None:
    parser = SseParser()

    events = parser.feed(b'id: 7\nevent: catalog\nfoo: bar\ndata: {"catalogRevision":"r"}\n\n')

    assert events == [SseEvent("catalog", {"catalogRevision": "r"})]


def test_an_event_without_name_defaults_to_message_like_sse() -> None:
    parser = SseParser()

    assert parser.feed(b'data: {"a":1}\n\n') == [SseEvent("message", {"a": 1})]


def test_malformed_json_is_skipped_and_the_stream_continues() -> None:
    parser = SseParser()

    events = parser.feed(
        b'event: values\ndata: {not json\n\nevent: catalog\ndata: {"catalogRevision":"r"}\n\n'
    )

    assert events == [SseEvent("catalog", {"catalogRevision": "r"})]


def test_a_non_object_payload_is_skipped() -> None:
    parser = SseParser()

    assert parser.feed(b"event: values\ndata: [1,2]\n\n") == []


def test_a_large_snapshot_line_beyond_aiohttps_readline_limit_is_parsed() -> None:
    # A plant with 4 packs x 32 cells sends a ~100 kB snapshot on ONE data line; aiohttp's
    # readline() refuses lines above 128 kB, so the parser must do its own splitting.
    values = ",".join(f'{{"id":"{i:036d}","value":{i},"ts":null}}' for i in range(4000))
    payload = (
        f'event: snapshot\ndata: {{"catalogRevision":"r","devices":[],"values":[{values}]}}\n\n'
    )
    parser = SseParser()

    events = parser.feed(payload.encode())

    assert len(events) == 1
    assert len(events[0].data["values"]) == 4000


def test_an_unterminated_line_beyond_the_cap_is_a_protocol_error() -> None:
    parser = SseParser(max_event_bytes=1024)

    with pytest.raises(SseProtocolError):
        parser.feed(b"data: " + b"x" * 2048)


def test_many_data_lines_of_one_event_beyond_the_cap_are_a_protocol_error() -> None:
    # Each line stays below the line cap, but the event never ends: the total is capped too.
    parser = SseParser(max_event_bytes=1024)
    for _ in range(9):
        parser.feed(b"data: " + b"x" * 110 + b"\n")  # 999 bytes held, below the cap

    with pytest.raises(SseProtocolError):
        parser.feed(b"data: " + b"x" * 110 + b"\n")


def test_a_multi_line_event_below_the_cap_is_parsed() -> None:
    # Positive counterpart: the same parser and cap accept an event of several data lines.
    parser = SseParser(max_event_bytes=1024)
    chunk = b'event: values\ndata: {"a":\ndata: "' + b"x" * 700 + b'"}\n\n'

    assert parser.feed(chunk) == [SseEvent("values", {"a": "x" * 700})]
