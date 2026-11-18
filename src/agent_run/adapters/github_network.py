"""GitHub 网络错误文字的唯一登记处；分类优先级由各入口决定。"""
from __future__ import annotations

import re


_NETWORK_MARKERS = (
    "dial tcp",
    "no such host",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "i/o timeout",
    "TLS handshake timeout",
)
_PATTERN = "|".join(re.escape(marker) for marker in _NETWORK_MARKERS)
_TEXT_PATTERN = re.compile(_PATTERN, re.IGNORECASE)
_BYTE_PATTERN = re.compile(_PATTERN.encode("utf-8"), re.IGNORECASE)
# 字节流保留最长文字减一的尾部，新增较长文字无需修改探针。
NETWORK_OVERLAP_BYTES = max(len(marker.encode("utf-8")) for marker in _NETWORK_MARKERS) - 1


def has_network_failure(diagnostic: str | bytes) -> bool:
    if isinstance(diagnostic, str):
        return _TEXT_PATTERN.search(diagnostic) is not None
    return _BYTE_PATTERN.search(diagnostic) is not None
