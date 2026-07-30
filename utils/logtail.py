"""Shared primitive for readers that tail a Minecraft server log mid-write.

Two independent readers each need to know "how much of what I just read is
safe to act on, given the writer might still be mid-flush on the last line":

  - ``core.logwatch.LogWatcher`` — a persistent, event-driven tailer. Its read
    position only ever moves forward, so it must NOT advance past a trailing
    partial line — there is no future re-read of the same range to catch what
    would otherwise be lost.
  - ``backends.bedrock.BedrockBackend.files_ready`` (via ``_parse_query_listing``)
    — a stateless poller that re-reads the same fixed start-of-command anchor
    to the current end every ~1s. It can safely DROP a trailing partial line,
    because the next poll re-reads the whole range again once the write
    completes.

They differ in what they do with a trailing partial line (hold the read
position back vs. drop and retry), but not in how to FIND the boundary
between complete and partial — that part lives here once instead of twice.
A missed boundary check in the persistent tailer is what let a genuine BDS
confirmation line ("Changes to the world are resumed.") split across two
writes go undetected on 2026-07-30, under load from a burst of concurrent
chat lines.
"""


def split_complete_lines(data: bytes) -> tuple[bytes, bytes]:
    """Split ``data`` at its last newline: ``(complete, leftover)``.

    ``complete`` always ends with ``b"\\n"`` (or is ``b""``) and is safe to
    decode and process as whole lines. ``leftover`` is everything after the
    last newline — a trailing partial line with no newline yet, which must
    not be parsed (a token or phrase cut mid-write would be wrong, not just
    incomplete).
    """
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return b"", data
    return data[:last_nl + 1], data[last_nl + 1:]
