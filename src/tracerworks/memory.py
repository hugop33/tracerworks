"""The heap handed back to the system after each cycle of `serve` (#2195).

CPython frees its objects, but the C allocator keeps the pages they lived in:
measured on 01/11, `serve` held 70 MB of heap for 3 MB of live objects — the
high-water mark of parsing `gh`'s large answers, in several threads. glibc's
`malloc_trim` gives the free pages back; elsewhere, nothing is done.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
from collections.abc import Callable


@functools.cache
def _trim() -> Callable[[int], int] | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    try:
        trim: Callable[[int], int] = ctypes.CDLL(name).malloc_trim
    except (OSError, AttributeError):  # not glibc
        return None
    return trim


def release_memory() -> None:
    """The free heap back to the system; never fails."""
    trim = _trim()
    if trim is not None:
        trim(0)
