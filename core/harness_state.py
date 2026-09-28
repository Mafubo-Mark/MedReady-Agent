from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


class Status(str, Enum):
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    SEARCHING = 'SEARCHING'
    EXTRACTING = 'EXTRACTING'
    FINISHED = 'FINISHED'
    FAILED = 'FAILED'


@dataclass
class TaskState:
    hospital: str
    service: str
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: Status = Status.PENDING
    search_results: list[dict] = field(default_factory=list)
    context_text: str = ''
    final_result: dict | None = None
    error_msg: str = ''

    def update_status(self, status: Status):
        self.status = status

    def append_context(self, text: str, limit: int = 20000):
        self.context_text = (self.context_text + '\n' + text)[-limit:]

    def get_context(self) -> str:
        return self.context_text

