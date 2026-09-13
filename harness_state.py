"""Phase 1-2: in-memory task state for MedReady Agent (Python 3.10+).

Create one TaskState per preparation request. The runtime updates it while
searching, extracting text, and preparing a result. This module does not call
APIs, write logs, save data to disk, or validate the final checklist schema.

Example::

    from core.harness_state import TaskState, TaskStatus

    state = TaskState(hospital="Example Hospital", service="MRI")
    state.update_status(TaskStatus.SEARCHING)
    state.append_context("Reference text extracted from a hospital page.")
    reference_text = state.get_context()

Context is limited by CHARACTER count, not model tokens. The 12,000-character
default is an application limit, not a guarantee that a model request fits.
The LLM module must also budget for prompts, input tokens, and output tokens.
Append pages in priority order: earlier text is retained when the limit is
reached. Truncation can cut a sentence or omit later pages; missing material
must never be treated as complete evidence.

Use append_context() to change context_text and update_status() to change
status. The runtime owns transition order, including retries and cache hits.
If tools run concurrently, have the runtime merge their results sequentially;
TaskState does not provide locks for concurrent changes to the same object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

__all__ = ["TaskStatus", "TaskState"]


class TaskStatus(str, Enum):
    """The six task stages defined in the MedReady development plan."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SEARCHING = "SEARCHING"
    EXTRACTING = "EXTRACTING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


@dataclass
class TaskState:
    """Hold the inputs, intermediate data, and outcome of ONE request.

    Required inputs are hospital and service; each must be a non-empty string.
    max_context_chars must be a positive integer and is set at construction.

    search_results holds page records supplied by the search tool. Keep source
    titles and URLs there for attribution. final_result starts as None and is
    assigned by the runtime after output validation. Mutable fields are created
    separately for each task, so new tasks never share their result lists.

    Data lasts only as long as this object; this is not the results cache.
    Request and result contents are omitted from the default object repr.
    """

    hospital: str = field(repr=False)
    service: str = field(repr=False)
    max_context_chars: int = 12_000
    task_id: str = field(default_factory=lambda: uuid4().hex, init=False)
    status: TaskStatus = field(default=TaskStatus.PENDING, init=False)
    search_results: list[dict[str, Any]] = field(
        default_factory=list, init=False, repr=False
    )
    context_text: str = field(default="", init=False, repr=False)
    final_result: dict[str, Any] | None = field(default=None, init=False, repr=False)
    error_msg: str = field(default="", init=False, repr=False)
    context_truncated: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Validate configuration before this task starts processing."""
        for name in ("hospital", "service"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string.")
            if not value.strip():
                raise ValueError(f"{name} must not be empty.")
            setattr(self, name, value.strip())

        if isinstance(self.max_context_chars, bool) or not isinstance(
            self.max_context_chars, int
        ):
            raise TypeError("max_context_chars must be an integer.")
        if self.max_context_chars <= 0:
            raise ValueError("max_context_chars must be greater than zero.")

    def update_status(
        self, status: TaskStatus | str, error_msg: str | None = None
    ) -> None:
        """Update the stage, accepting an enum or its exact uppercase value.

        For failure, use update_status(TaskStatus.FAILED, error_msg="...").
        Store a short, safe explanation rather than raw responses or secrets.
        Moving to a non-failed stage clears the previous error. A failed task
        clears final_result while retaining intermediate data for the runtime.
        Invalid arguments raise before changing any state.
        """
        new_status = TaskStatus(status)
        if error_msg is not None and not isinstance(error_msg, str):
            raise TypeError("error_msg must be a string or None.")
        if error_msg is not None and new_status is not TaskStatus.FAILED:
            raise ValueError("Only FAILED status accepts an error message.")

        self.status = new_status
        if new_status is TaskStatus.FAILED:
            self.error_msg = error_msg or ""
            self.final_result = None
        else:
            self.error_msg = ""

    def append_context(self, text: str) -> None:
        """Append non-empty page text without exceeding max_context_chars.

        Leading and trailing whitespace is removed. Pages are separated by a
        blank line, which also counts toward the limit. Once text is omitted,
        context_truncated remains True for the rest of the task. Empty text
        is ignored; non-string inputs raise TypeError.
        """
        if not isinstance(text, str):
            raise TypeError("Context text must be a string.")
        text = text.strip()
        if not text:
            return

        separator = "\n\n" if self.context_text else ""
        available = self.max_context_chars - len(self.context_text) - len(separator)
        if available <= 0:
            self.context_truncated = True
            return

        self.context_text += separator + text[:available]
        if len(text) > available:
            self.context_truncated = True

    def get_context(self) -> str:
        """Return the collected reference text without changing the state."""
        return self.context_text


if __name__ == "__main__":
    # Run from the project root: python3 -m core.harness_state
    state = TaskState(hospital="Example Hospital", service="MRI")
    print(f"Task ID: {state.task_id}")
    print(f"Initial status: {state.status.value}")

    for step in (TaskStatus.RUNNING, TaskStatus.SEARCHING, TaskStatus.EXTRACTING):
        state.update_status(step)
        print(f"Current status: {state.status.value}")

    state.search_results.append(
        {"title": "Example visit guide", "url": "https://example.com/visit-guide"}
    )
    state.append_context("Example reference text from page A.")
    state.append_context("Example reference text from page B.")
    print(f"Collected context:\n{state.get_context()}")
    print(f"Context truncated: {state.context_truncated}")

    # Only a state-storage example, not a validated preparation checklist.
    state.final_result = {"demo": "The runtime would store its validated result here."}
    state.update_status(TaskStatus.FINISHED)
    print(f"Final status: {state.status.value}")

    failed = TaskState(hospital="Example Hospital", service="MRI")
    failed.update_status(TaskStatus.FAILED, error_msg="Search service unavailable.")
    print(f"Failure example: {failed.status.value} - {failed.error_msg}")

    bounded = TaskState(hospital="Example Hospital", service="MRI", max_context_chars=10)
    bounded.append_context("0123456789 EXTRA TEXT")
    print(f"Bounded context: {bounded.get_context()!r}")
    print(f"Truncation detected: {bounded.context_truncated}")

    fresh = TaskState(hospital="Another Hospital", service="Outpatient visit")
    assert fresh.task_id != state.task_id
    assert fresh.search_results == []
    assert fresh.get_context() == ""
    assert fresh.final_result is None
    assert bounded.get_context() == "0123456789"
    assert bounded.context_truncated
    print("PASS: task data is separate and the context limit works.")
