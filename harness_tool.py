"""Phase 1-4: tool execution and registration for MedReady Agent.
阶段 1-4：诊前智备工具调用与注册管理。

Implement BaseTool.run(params) in search_baidu.py and html_extractor.py.
Call execute() or ToolHarness.call() to get timeout/error/logging protection.
在搜索和网页提取模块中实现 BaseTool.run(params)。
通过 execute() 或 ToolHarness.call() 调用，才能应用超时、异常和日志管理。

Every managed call returns / 每次受管理的调用返回：
    {"success": bool, "data": any, "error": str, "time_cost": float}

run() returns the RAW data dictionary; the harness wraps it once. For example,
a search tool returns {"results": [...]}. Raise ToolError for a known failure;
an empty result list by itself is a successful search with no matches.
run() 返回原始数据字典，由 Harness 统一包装。搜索工具可返回 {"results": [...]}。
已知失败请抛出 ToolError；空结果列表本身代表搜索成功但没有匹配项。

Default caller timeout: 10 seconds. This is a WAITING deadline, not forced
cancellation. A timed-out worker can continue (including an API request).
Each tool instance permits at most four concurrent workers by default, with
no waiting queue. A worker retains its slot until it actually ends. Reuse tool
instances; do not repeatedly construct tools to bypass this limit.
默认调用等待超时为 10 秒，不会强制终止已运行的线程或撤销 API 请求。
每个工具实例默认最多同时运行 4 个任务，不排队；任务真正结束后才释放名额。
请复用工具实例，不要通过重复创建实例绕过限制。

Network tools MUST also set HTTP timeouts. Do not use these daemon workers
for irreversible writes or work that must finish during application shutdown.
Use process isolation if hard termination is required. This MVP supports
synchronous run() implementations only; async tools need a separate adapter.
网络工具仍须设置 HTTP 超时。守护线程不适合不可逆写操作或退出时必须完成的任务。
如需强制终止应使用进程隔离。本版本仅支持同步 run()，异步工具须另设适配器。

Depends only on the standard library and core.harness_logger. Never logs
parameters, returned data, exception messages, or credentials. ToolError
messages are exposed to callers and MUST be safe for users to read.
仅依赖标准库和 core.harness_logger，不记录参数、返回数据、异常原文或密钥。
ToolError 的消息会返回给调用方，必须使用不含敏感信息的用户提示。

Run built-in tests from the project root / 在项目根目录运行内置测试：
    python3 -m core.harness_tool
"""

from __future__ import annotations

import inspect
import math
import re
import threading
from abc import ABC, abstractmethod
from copy import deepcopy
from queue import Empty, Queue
from time import perf_counter
from typing import Any

from core.harness_logger import get_logger

__all__ = ["BaseTool", "ToolHarness", "ToolError"]


class ToolError(Exception):
    """An expected failure with a safe public message / 带安全提示的已知工具异常。"""


def _valid_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timeout must be numeric / 超时必须为数字。")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("Timeout is too large / 超时数值过大。") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be finite and positive / 超时必须为有限正数。")
    return float(value)


def _valid_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError(
            "Tool name must start with a letter and contain 1-64 ASCII letters, "
            "digits, underscores, dots or hyphens / 工具名须以英文字母开头，"
            "长度 1-64，仅含英文字母、数字、下划线、点或连字符。"
        )
    return name


def _result(started: float, success: bool, data: Any = None, error: str = "") -> dict:
    return {
        "success": success,
        "data": data if success else None,
        "error": "" if success else error,
        "time_cost": round(max(0.0, perf_counter() - started), 6),
    }


class BaseTool(ABC):
    """Base class for a reusable tool / 可复用工具的抽象基类。

    If a subclass defines __init__, call super().__init__(...). Callers should
    leave params unchanged until execute() returns. Each run receives a deep
    copy, so a worker cannot mutate the caller's nested parameter containers.
    子类如定义 __init__，须调用 super().__init__(...)。调用期间请勿修改 params。
    run() 收到深拷贝参数，避免工作线程修改调用方的嵌套参数容器。
    """

    def __init__(
        self, name: str | None = None, timeout: float = 10.0, max_concurrency: int = 4
    ) -> None:
        self._name = _valid_name(name if name is not None else type(self).__name__)
        self._timeout = _valid_timeout(timeout)
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer / 并发上限必须为正整数。")
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._logger = get_logger("core.harness_tool")

    @property
    def name(self) -> str:
        """Stable registration name / 固定注册名称。"""
        return self._name

    @property
    def timeout(self) -> float:
        """Default waiting deadline in seconds / 默认等待秒数。"""
        return self._timeout

    @abstractmethod
    def run(self, params: dict) -> dict:
        """Implement synchronous tool logic and return raw data / 实现同步工具逻辑并返回原始数据。"""
        raise NotImplementedError

    def _finish(self, result: dict, status: str) -> dict:
        # Log safe metadata only / 仅记录安全的调用元数据。
        log = self._logger.info if result["success"] else self._logger.warning
        log("tool=%s status=%s time_cost=%.6fs", self.name, status, result["time_cost"])
        return result

    def execute(self, params: dict, *, timeout: float | None = None) -> dict:
        """Run with validation, timeout, error handling and logs / 统一受控调用。

        time_cost is caller elapsed time in seconds, including worker startup.
        A per-call timeout override does not change the tool's default. A busy
        tool fails immediately; no extra work is queued or started.
        time_cost 是调用方经过的秒数，包含启动工作线程的时间。
        单次超时参数不改变默认值；工具繁忙时立即返回失败，不额外排队或启动任务。
        """
        started = perf_counter()
        if not isinstance(params, dict):
            return self._finish(_result(started, False, error="Tool parameters must be a dictionary / 工具参数必须为字典。"), "invalid_params")
        try:
            limit = _valid_timeout(self.timeout if timeout is None else timeout)
        except ValueError:
            return self._finish(_result(started, False, error="Invalid timeout / 超时参数无效。"), "invalid_timeout")
        if inspect.iscoroutinefunction(self.run) or inspect.isasyncgenfunction(self.run):
            return self._finish(_result(started, False, error="Async tools require an adapter / 异步工具需要适配器。"), "unsupported_async")
        if not self._slots.acquire(blocking=False):
            return self._finish(_result(started, False, error="Tool is busy; try again later / 工具繁忙，请稍后重试。"), "busy")

        # One queue per call prevents late results leaking into another request.
        # 每次调用独立队列，避免迟到的结果混入其他请求。
        outcomes: Queue = Queue(maxsize=1)

        def worker() -> None:
            data = None
            error = ""
            status = "success"
            try:
                data = self.run(deepcopy(params))
                if inspect.isawaitable(data):
                    if inspect.iscoroutine(data):
                        data.close()
                    data = None
                    status = "invalid_result"
                    error = "run() must return a dictionary, not an awaitable / run() 必须返回字典，不能返回待等待对象。"
                elif not isinstance(data, dict):
                    data = None
                    status = "invalid_result"
                    error = "run() must return a dictionary / run() 必须返回字典。"
                else:
                    data = deepcopy(data)
            except ToolError as exc:
                data = None
                status = "tool_error"
                error = str(exc) or "Tool could not complete the request / 工具未能完成请求。"
            except BaseException:
                # Worker-local failures must not expose raw exception content.
                # 不向调用方或日志暴露线程中的异常原文。
                data = None
                status = "exception"
                error = "Tool execution failed / 工具执行失败。"
            finally:
                finished = perf_counter()
                # Keep capacity occupied until actual completion, even on timeout.
                # 即使调用方已超时，也须等任务真正结束后释放名额。
                self._slots.release()
                outcomes.put_nowait((status, data, error, finished))

        thread = threading.Thread(target=worker, name=f"tool-{self.name}", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._slots.release()
            return self._finish(_result(started, False, error="Tool worker could not start / 无法启动工具线程。"), "start_failed")

        deadline = started + limit
        try:
            status, data, error, finished = outcomes.get(timeout=max(0.0, deadline - perf_counter()))
            if finished > deadline:
                raise Empty
        except Empty:
            return self._finish(
                _result(started, False, error=f"Tool timed out after {limit:g} seconds / 工具等待超过 {limit:g} 秒。"),
                "timeout",
            )
        return self._finish(_result(started, status == "success", data, error), status)


class ToolHarness:
    """Register instances once, then call them by name / 注册工具实例并按名称调用。

    Registry operations are thread-safe. Tools with shared mutable internal
    state must protect it themselves or use max_concurrency=1. Registration is
    not end-user authorization; the API/runtime must enforce access policies.
    注册表操作支持线程安全。工具内部共享可变状态须自行加锁或设并发上限为 1。
    注册不等于用户权限认证，访问权限仍由 API 或 Runtime 层控制。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._lock = threading.RLock()
        self._logger = get_logger("core.harness_tool")

    def register(self, tool: BaseTool) -> None:
        """Register without silently replacing an existing tool / 注册时禁止静默覆盖同名工具。"""
        if not isinstance(tool, BaseTool):
            raise TypeError("Tool must inherit BaseTool / 工具必须继承 BaseTool。")
        name = _valid_name(tool.name)
        with self._lock:
            if name in self._tools:
                raise ValueError(f"Tool already registered / 工具已注册：{name}")
            self._tools[name] = tool
        self._logger.info("tool=%s status=registered", name)

    def list_tools(self) -> list[str]:
        """Return a new list of registered names / 返回已注册名称的新列表。"""
        with self._lock:
            return sorted(self._tools)

    def call(self, name: str, params: dict, *, timeout: float | None = None) -> dict:
        """Call a registered tool using the standard result envelope / 按名称调用并返回统一格式。"""
        started = perf_counter()
        try:
            _valid_name(name)
        except ValueError:
            self._logger.warning("status=invalid_tool_name")
            return _result(started, False, error="Invalid tool name / 工具名称无效。")
        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            self._logger.warning("status=unregistered_tool")
            return _result(started, False, error="Tool is not registered / 工具未注册。")
        return tool.execute(params, timeout=timeout)


if __name__ == "__main__":
    import json

    class EchoTool(BaseTool):
        def run(self, params: dict) -> dict:
            return {"message": params.get("message", "Hello / 你好")}

    class FailingTool(BaseTool):
        def run(self, params: dict) -> dict:
            raise ToolError("Demo failure, not a real service error / 演示失败，并非真实服务故障。")

    class SlowTool(BaseTool):
        def run(self, params: dict) -> dict:
            threading.Event().wait(0.2)
            return {"message": "Late result / 迟到的结果"}

    harness = ToolHarness()
    harness.register(EchoTool(name="echo"))
    harness.register(FailingTool(name="failure"))
    harness.register(SlowTool(name="slow", max_concurrency=1))
    examples = [
        ("Success / 成功", harness.call("echo", {"message": "MedReady tools are ready / 工具已就绪"}), True),
        ("Failure / 失败", harness.call("failure", {}), False),
        ("Timeout / 超时", harness.call("slow", {}, timeout=0.02), False),
        ("Unknown tool / 未注册工具", harness.call("missing", {}), False),
    ]
    for label, result, expected in examples:
        assert result["success"] is expected
        assert set(result) == {"success", "data", "error", "time_cost"}
        print(f"\n{label}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\nPASS / 通过：Registration, execution, errors and timeout / 注册、执行、异常与超时。")
    print("This demo uses fixed examples and does not request input / 本演示使用固定示例，不等待输入。")
