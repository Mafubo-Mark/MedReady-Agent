"""Phase 1-1: logging and function timing for MedReady Agent.

Usage from another project module::

    from core.harness_logger import get_logger, log_time

    logger = get_logger(__name__)

    @log_time
    def prepare():
        logger.info("Preparation started")

Logs go to the console and <project root>/logs/medready.log in UTF-8.
The file rotates at local midnight, on the next emitted log record.
Dated archives are retained for up to 14 rotations. All four requested
levels (DEBUG, INFO, WARNING, ERROR) are enabled by default.

This file handler supports threads in a single process. Multiple processes
must not share this rotating file; use a logging listener or separate files
before deploying multiple workers or enabling logging in a separate frontend.

The timing decorator records function names, durations, and outcomes only.
It does not record arguments, return values, or exception messages. Callers
must likewise avoid putting credentials or patient information in log messages.
"""

from __future__ import annotations

import inspect
import logging
import threading
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, TypeVar, cast

__all__ = ["get_logger", "log_time"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIRECTORY = _PROJECT_ROOT / "logs"
_LOGGER_NAME = "medready"
_CONFIG_LOCK = threading.RLock()
_HANDLER_MARKER = "_medready_harness_handler"
_F = TypeVar("_F", bound=Callable[..., Any])


def get_logger(name: str) -> logging.Logger:
    """Return a named logger sharing the project's console and file handlers.

    Pass ``__name__`` to identify the calling module. Initialization is lazy,
    thread-safe, and does not modify Python's root logger. An unwritable logs
    directory raises an OSError instead of silently disabling file logging.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Logger name must be a non-empty string.")

    with _CONFIG_LOCK:
        parent = logging.getLogger(_LOGGER_NAME)
        if not any(getattr(h, _HANDLER_MARKER, False) for h in parent.handlers):
            _LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler = TimedRotatingFileHandler(
                filename=_LOG_DIRECTORY / "medready.log",
                when="midnight",
                interval=1,
                backupCount=14,
                encoding="utf-8",
                utc=False,
            )
            console_handler = logging.StreamHandler()
            for handler in (console_handler, file_handler):
                handler.setLevel(logging.DEBUG)
                handler.setFormatter(formatter)
                setattr(handler, _HANDLER_MARKER, True)
                parent.addHandler(handler)

            parent.setLevel(logging.DEBUG)
            # Avoid duplicate output through Uvicorn or other root handlers.
            parent.propagate = False

        full_name = (
            name
            if name == _LOGGER_NAME or name.startswith(_LOGGER_NAME + ".")
            else f"{_LOGGER_NAME}.{name}"
        )
        return logging.getLogger(full_name)


def _record_duration(
    logger: logging.Logger, function_name: str, started: float, succeeded: bool
) -> None:
    """Use a monotonic clock so clock adjustments do not affect timings."""
    logger.log(
        logging.INFO if succeeded else logging.ERROR,
        "%s %s in %.4f seconds",
        function_name,
        "completed" if succeeded else "did not complete",
        perf_counter() - started,
    )


def log_time(func: _F) -> _F:
    """Use ``@log_time`` to time a regular or async function.

    Preserves function metadata, return values, and exceptions. Failed or
    cancelled calls also produce a timing record. Async functions are timed
    through completion of the await, rather than just coroutine creation.
    Generator functions are rejected because their execution is deferred.
    """
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        raise TypeError("log_time supports regular and async functions, not generators.")

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = get_logger(func.__module__)
            started = perf_counter()
            succeeded = False
            try:
                result = await func(*args, **kwargs)
                succeeded = True
                return result
            finally:
                _record_duration(logger, func.__qualname__, started, succeeded)

        return cast(_F, async_wrapper)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        logger = get_logger(func.__module__)
        started = perf_counter()
        succeeded = False
        try:
            result = func(*args, **kwargs)
            succeeded = True
            return result
        finally:
            _record_duration(logger, func.__qualname__, started, succeeded)

    return cast(_F, sync_wrapper)


if __name__ == "__main__":
    # Run from the project root: python -m core.harness_logger
    demo_logger = get_logger("logger_demo")
    demo_logger.debug("DEBUG logging is working.")
    demo_logger.info("INFO logging is working.")
    demo_logger.warning("WARNING demonstration; no actual problem.")
    demo_logger.error("ERROR demonstration; no actual failure.")

    @log_time
    def demo_task() -> int:
        """A small local task; no API calls or credentials required."""
        return sum(range(10_000))

    demo_task()
    demo_logger.info("Log file: %s", _LOG_DIRECTORY / "medready.log")
