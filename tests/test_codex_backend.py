"""The live Worker JSONL buffer retains thread/error evidence within its byte cap."""
from agent_run.worker_sandbox import _BoundedJsonlStream


def test_jsonl_stream_uses_fixed_chunks_and_keeps_thread_and_error_tail() -> None:
    lines: list[str] = []
    stream = _BoundedJsonlStream(line_bytes=1024, on_line=lines.append)
    stream.feed(b'{"type":"thread.started","thread_id":"thread-189"}\n')
    noise_chunk = b"noise" * 1000
    for _ in range(500):
        stream.feed(noise_chunk)
    stream.feed(
        b'\n{"type":"turn.failed","error":{"message":"bounded failure"}}\n'
    )
    stream.finish()

    captured = '\n'.join(lines)
    assert "thread-189" in captured
    assert "bounded failure" in captured
    assert len(captured.encode("utf-8")) <= 4096
