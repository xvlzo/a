import queue
import dataclasses
import enum


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

    def to_dict(self):
        return {
            "category": self.category,
            "artifact": self.artifact,
            "status": self.status.value,
            "detail": self.detail,
        }


class StatusReporter:
    def __init__(self, q: queue.Queue):
        self._q = q

    def emit(self, category: str, artifact: str, status: Status, detail: str = "") -> None:
        self._q.put(Event(category, artifact, status, detail))

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


class BaseCleaner:
    CATEGORY: str = ""

    def run(self, paths: list, reporter: StatusReporter) -> None:
        raise NotImplementedError
