"""
Fixed-size circular buffer for recent history (used in smoothing and velocity estimation).
"""
from collections import deque
from typing import Any, Deque, Optional


class RingBuffer:
    def __init__(self, maxlen: int):
        self._buf: Deque[Any] = deque(maxlen=maxlen)

    def push(self, item: Any) -> None:
        self._buf.append(item)

    def __len__(self) -> int:
        return len(self._buf)

    def __getitem__(self, i: int) -> Any:
        return self._buf[i]

    @property
    def newest(self) -> Optional[Any]:
        return self._buf[-1] if self._buf else None

    @property
    def oldest(self) -> Optional[Any]:
        return self._buf[0] if self._buf else None

    def to_list(self):
        return list(self._buf)

    def clear(self):
        self._buf.clear()
