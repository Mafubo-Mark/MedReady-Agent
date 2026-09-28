from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from time import monotonic
from core.harness_logger import get_logger


class BaseTool(ABC):
    name = 'base'
    timeout = 10

    @abstractmethod
    def run(self, params: dict):
        raise NotImplementedError

    def invoke(self, params: dict) -> dict:
        started = monotonic()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.run, params)
        try:
            data = future.result(timeout=self.timeout)
            return {'success': True, 'data': data, 'error': '', 'time_cost': round(monotonic() - started, 3)}
        except FutureTimeout:
            future.cancel()
            return {'success': False, 'data': None, 'error': 'Tool timed out', 'time_cost': round(monotonic() - started, 3)}
        except Exception as exc:
            get_logger(__name__).warning('%s failed: %s', self.name, type(exc).__name__)
            return {'success': False, 'data': None, 'error': str(exc), 'time_cost': round(monotonic() - started, 3)}
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self.tools[tool.name] = tool

    def call(self, name: str, params: dict) -> dict:
        return self.tools[name].invoke(params)

