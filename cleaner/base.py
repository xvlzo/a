import dataclasses
import enum
import queue
import threading
import time
from typing import Optional


class Status(enum.Enum):
    RUNNING = "running"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    SKIP = "skip"


@dataclasses.dataclass
class Event:
    category: str
    artifact: str
    status: Status
    detail: str = ""
    timestamp: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "artifact": self.artifact,
            "status": self.status.value,
            "detail": self.detail,
            "ts": round(self.timestamp, 3),
        }


class StatusReporter:
    def __init__(self, q: queue.Queue) -> None:
        self._q = q
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def emit(self, category: str, artifact: str, status: Status, detail: str = "") -> None:
        ev = Event(category=category, artifact=artifact, status=status, detail=detail)
        with self._lock:
            self._events.append(ev)
        self._q.put(ev)

    def running(self, category: str, artifact: str) -> None:
        self.emit(category, artifact, Status.RUNNING)

    def ok(self, category: str, artifact: str, detail: str = "") -> None:
        self.emit(category, artifact, Status.OK, detail)

    def warn(self, category: str, artifact: str, detail: str = "") -> None:
        self.emit(category, artifact, Status.WARN, detail)

    def error(self, category: str, artifact: str, detail: str = "") -> None:
        self.emit(category, artifact, Status.ERROR, detail)

    def skip(self, category: str, artifact: str, detail: str = "") -> None:
        self.emit(category, artifact, Status.SKIP, detail)

    def get_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)


class BaseCleaner:
    CATEGORY: str = ""

    def run(self, paths: list[str], reporter: StatusReporter) -> None:
        raise NotImplementedError
