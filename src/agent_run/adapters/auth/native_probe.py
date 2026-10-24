"""Stream native gh auth evidence without retaining arbitrary headers or bodies."""
from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess

from agent_run.runtime.process_io import TailOutput
from agent_run.runtime.command import capture_command


@dataclass(frozen=True)
class AuthProbeCapture:
    returncode: int
    timed_out: bool
    http_status: int | None
    rate_limited: bool
    network_failure: bool


class _HeaderEvidence(TailOutput):
    def __init__(self) -> None:
        super().__init__(0)
        self.http_status: int | None = None
        self.rate_limited = False
        self._first = True
        self._done = False
        self._prefix = bytearray()
        self._colon = False
        self._field = ""
        self._value = 0
        self._non_cr = 0

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._done:
                return
            if byte == 10:
                self._end_line()
                continue
            if byte != 13:
                self._non_cr = min(2, self._non_cr + 1)
            if self._first:
                if len(self._prefix) < 32:
                    self._prefix.append(byte)
            elif not self._colon:
                if byte == 58:
                    name = bytes(self._prefix).lower()
                    self._field = ("remaining" if name == b"x-ratelimit-remaining" else
                                   "retry" if name == b"retry-after" else "")
                    self._prefix.clear()
                    self._colon = True
                elif len(self._prefix) < 32:
                    self._prefix.append(byte)
            elif self._field == "retry":
                if byte not in (9, 13, 32):
                    self._value = 1
            elif self._field == "remaining":
                if byte in (9, 13, 32):
                    if self._value == 1:
                        self._value = 2
                elif byte == 48 and self._value in (0, 1):
                    self._value = 1
                else:
                    self._value = 3

    def _end_line(self) -> None:
        if self._first:
            match = re.match(rb"HTTP/[0-9.]+ ([1-5][0-9]{2})(?:[ \r]|$)", bytes(self._prefix))
            self.http_status = int(match[1]) if match else None
            self._done = match is None
            self._first = False
        elif self._non_cr == 0:
            self._done = True  # Never interpret a body as another status/header.
        elif (self._field == "retry" and self._value == 1
              or self._field == "remaining" and self._value in (1, 2)):
            self.rate_limited = True
        self._prefix.clear()
        self._colon = False
        self._field = ""
        self._value = 0
        self._non_cr = 0


class _ErrorEvidence(TailOutput):
    def __init__(self) -> None:
        super().__init__(0)
        self.network_failure = False
        self.rate_limited = False
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        # The rolling overlap recognizes fixed tokens across pipe chunks only.
        window = self._tail + chunk
        self.network_failure |= re.search(
            rb"dial tcp|no such host|network is unreachable|connection (?:refused|reset)|i/o timeout|TLS handshake timeout",
            window, re.IGNORECASE,
        ) is not None
        self.rate_limited |= re.search(rb"secondary rate limit|rate limit exceeded", window, re.IGNORECASE) is not None
        self._tail = window[-64:]


def capture_auth_probe(process: subprocess.Popen[bytes], *, timeout: float) -> AuthProbeCapture:
    """Collect safe stream metadata under the one existing command lifecycle."""
    headers, errors = _HeaderEvidence(), _ErrorEvidence()
    result = capture_command(process, timeout=timeout, max_output=0, stdout=headers, stderr=errors)
    return AuthProbeCapture(result.returncode, result.timed_out, headers.http_status,
                            headers.rate_limited or errors.rate_limited, errors.network_failure)
