"""One transient write protocol; physical evidence never grants Core authority."""
from collections.abc import Callable
from dataclasses import dataclass

FinalAdmission = Callable[[], None]


@dataclass(frozen=True)
class WriteAuthority:
    """Fresh slow preparation returns an unexecuted, no-I/O Core admission."""
    prepare: Callable[[], FinalAdmission]


def admit_write(authority: WriteAuthority, boundary: Callable[[], None] | None = None) -> None:
    """Run after command preparation, immediately before the physical request."""
    final_admission = authority.prepare()
    if boundary is not None:
        boundary()
    final_admission()
