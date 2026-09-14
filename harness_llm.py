"""Phase 2-3: DeepSeek LLM management / 阶段 2-3：DeepSeek 大模型调用管理。

Public API / 公共接口:
    llm = MedReadyLLMHarness()
    answer = llm.chat(system_prompt, user_prompt, json_mode=False)  # str

Reads config/.env relative to the project, regardless of the working directory.
Existing process environment variables take priority. ENV selects dev or prod;
a missing selected key never falls back to the other environment's key.
按项目路径读取 config/.env，系统环境变量优先；所选环境缺少密钥时不会使用另一环境的密钥。

Required settings / 必需配置:
    ENV=dev
    DEEPSEEK_API_KEY_DEV=<your development key>
    DEEPSEEK_API_KEY_PROD=<your production key>
    DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
    DEEPSEEK_MODEL=deepseek-chat
Model names are read from your configuration; use a model available to your account.
模型名称从配置读取，请使用账号当前支持的模型。

Transient network/server errors and invalid/empty model responses receive two
retries, one second apart (three attempts total). Configuration/authentication
errors fail immediately. Retries may consume additional API tokens.
临时网络/服务器错误及空白或无效响应最多重试两次，间隔一秒；配置/鉴权错误直接报错。
重试可能额外消耗 token。HTTP connect/read timeouts are not a total-call deadline.
HTTP 连接/读取超时不等于整个调用的总时限。

Logs metadata and provider-reported token counts, never keys, prompts or answers.
usage_totals counts all reported usage, including rejected responses; missing
usage cannot be estimated and is logged as unavailable. Counters are per instance.
仅记录元数据及服务端报告的 token 数，不记录密钥、提示词或回答。累计统计包含被拒绝响应
中已报告的用量；缺失用量不做估算。统计仅限当前实例，不是账单。

This is separate from BaseTool. Runtime owns state updates and compliance checks.
JSON mode validates syntax/object type, not the Phase 2-4 business schema.
本模块独立于 BaseTool；Runtime 负责状态更新及合规校验。JSON 模式只验证语法及对象类型，
不替代阶段 2-4 的业务字段校验。

Run from the project root / 在项目根目录运行:
    python3 -m core.harness_llm --demo
    python3 -m core.harness_llm --interactive
    python3 -m core.harness_llm --interactive --json

Dependencies: requests, python-dotenv, core.harness_logger (already in project).
Reference: https://api-docs.deepseek.com/guides/json_mode/
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import dotenv_values
from core.harness_logger import get_logger

__all__ = ["MedReadyLLMHarness", "LLMError"]
_ENV_PATH = Path(__file__).resolve().parents[1] / "config" / ".env"
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class LLMError(RuntimeError):
    """Safe caller-facing failure / 可向用户展示的异常，不包含原始响应。"""


def _strict_json(text: str):
    def reject_constant(value):
        raise ValueError("Non-standard JSON constant")
    return json.loads(text, parse_constant=reject_constant)


class MedReadyLLMHarness:
    """Synchronous DeepSeek client / 同步 DeepSeek 客户端。"""

    def __init__(self, *, env_path: str | Path = _ENV_PATH,
                 timeout: float = 60.0, max_tokens: int = 4096):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite / 超时必须为有限正数。")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer / max_tokens 必须为正整数。")
        path = Path(env_path)
        try:
            config = dotenv_values(path, interpolate=False) if path.is_file() else {}
        except (OSError, UnicodeError):
            raise LLMError("Cannot read config/.env / 无法读取环境配置文件。") from None

        def setting(name, default=""):
            return str(os.environ.get(name, config.get(name) or default)).strip()

        self.environment = setting("ENV", "dev").lower()
        if self.environment not in ("dev", "prod"):
            raise LLMError("ENV must be dev or prod / ENV 必须为 dev 或 prod。")
        self._api_key = setting(f"DEEPSEEK_API_KEY_{self.environment.upper()}")
        if not self._api_key or any(c.isspace() for c in self._api_key) or not self._api_key.isascii():
            raise LLMError("Set the selected DEEPSEEK_API_KEY_DEV/PROD in config/.env / 请配置所选环境的 DeepSeek 密钥。")
        base = setting("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        # Only send credentials to the official endpoint / 密钥仅发送至官方接口。
        if base not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            raise LLMError("DEEPSEEK_BASE_URL must be https://api.deepseek.com or https://api.deepseek.com/v1 / 请使用官方接口地址。")
        self._endpoint = base + "/chat/completions"
        self.model = setting("DEEPSEEK_MODEL", "deepseek-chat")
        self.timeout = float(timeout)
        self.max_tokens = max_tokens
        self._logger = get_logger("core.harness_llm")
        self._lock = threading.Lock()
        self._usage = dict.fromkeys(_TOKEN_FIELDS, 0)

    @property
    def usage_totals(self) -> dict:
        """Copy of reported cumulative counts / 返回已报告累计用量的副本。"""
        with self._lock:
            return dict(self._usage)

    def _record_usage(self, data: dict, attempt: int) -> None:
        usage = data.get("usage")
        values = {key: usage.get(key) for key in _TOKEN_FIELDS} if isinstance(usage, dict) else {}
        values = {key: value for key, value in values.items()
                  if type(value) is int and value >= 0}
        with self._lock:
            for key, value in values.items():
                self._usage[key] += value
        self._logger.info(
            "llm attempt=%d prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            attempt, *(values.get(key, "unavailable") for key in _TOKEN_FIELDS))

    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        """Return answer text; raise LLMError on failure / 返回回答字符串；失败抛出 LLMError。"""
        if not isinstance(system_prompt, str) or not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("Prompts must be strings and user_prompt nonempty / 提示词必须为字符串，用户提示不能为空。")
        if not isinstance(json_mode, bool):
            raise ValueError("json_mode must be a boolean / json_mode 必须为布尔值。")
        if json_mode:
            system_prompt += ('\nReturn exactly one valid JSON object, without Markdown. '
                              'Follow the requested fields; if none are specified, use '
                              'this example structure: {"answer": "your answer"}.')
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}],
            "stream": False, "max_tokens": self.max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        error = "LLM request failed / 大模型调用失败。"
        for attempt in range(1, 4):
            try:
                # No redirect may forward credentials / 禁止通过重定向转发密钥。
                with requests.post(self._endpoint, headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json"}, json=payload,
                    timeout=(min(10.0, self.timeout), self.timeout),
                    allow_redirects=False) as response:
                    status = response.status_code
                    if status != 200:
                        if status not in (408, 429) and not 500 <= status <= 599:
                            self._logger.warning("llm status=http_error http_status=%d attempt=%d", status, attempt)
                            raise LLMError(f"DeepSeek HTTP {status}: check key, balance, model and configuration / 请检查密钥、余额、模型及配置。")
                        error = f"DeepSeek temporarily unavailable (HTTP {status}) / DeepSeek 暂时不可用。"
                    else:
                        try:
                            data = response.json()
                            if not isinstance(data, dict):
                                raise ValueError
                            self._record_usage(data, attempt)
                            choice = data["choices"][0]
                            content = choice["message"]["content"]
                            if choice.get("finish_reason") != "stop":
                                raise ValueError
                            if not isinstance(content, str) or not content.strip():
                                raise ValueError
                            if json_mode and not isinstance(_strict_json(content), dict):
                                raise ValueError
                        except (ValueError, KeyError, IndexError, TypeError):
                            error = "Model response is empty, incomplete or invalid; check prompt and max_tokens / 模型响应为空、不完整或无效，请检查提示词及 max_tokens。"
                        else:
                            self._logger.info("llm status=success environment=%s attempt=%d time_cost=%.6fs",
                                              self.environment, attempt, time.perf_counter() - started)
                            return content
            except (requests.Timeout, requests.ConnectionError):
                error = "DeepSeek connection failed or timed out / DeepSeek 连接失败或超时。"
            except requests.RequestException:
                raise LLMError("Unable to send DeepSeek request / 无法发送 DeepSeek 请求。") from None
            if attempt < 3:
                self._logger.warning("llm status=retry attempt=%d delay_seconds=1", attempt)
                time.sleep(1)
        self._logger.warning("llm status=failed attempts=3 time_cost=%.6fs", time.perf_counter() - started)
        raise LLMError(error) from None


def _demo() -> int:
    """Mock requests; no real key, network or charges / 模拟请求，不使用真实密钥、网络或费用。"""
    from tempfile import TemporaryDirectory
    from unittest.mock import MagicMock, patch

    def response(content, status=200):
        r = MagicMock()
        r.__enter__.return_value = r
        r.status_code = status
        r.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return r

    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "ENV": "dev", "DEEPSEEK_API_KEY_DEV": "offline-placeholder",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1", "DEEPSEEK_MODEL": "demo-model"}):
        llm = MedReadyLLMHarness(env_path=Path(directory) / ".env")
        with patch.object(requests, "post", return_value=response("Hello / 你好")):
            assert llm.chat("Be helpful", "Say hello") == "Hello / 你好"
        with patch.object(requests, "post", side_effect=[response("", 503), response('{"answer":"你好"}')]) as post, patch.object(time, "sleep") as sleep:
            answer = llm.chat("Be helpful", "Say hello", json_mode=True)
            assert json.loads(answer) == {"answer": "你好"}
            assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}
            sleep.assert_called_once_with(1)
        with patch.object(requests, "post", return_value=response("not JSON")) as post, patch.object(time, "sleep"):
            try:
                llm.chat("Be helpful", "Return JSON", True)
            except LLMError:
                assert post.call_count == 3
            else:
                raise AssertionError("Invalid JSON accepted")
        assert llm.usage_totals["total_tokens"] == 75
    print("PASS / 通过：text, JSON, retries, invalid output and token accounting / 文本、JSON、重试、无效输出及用量统计。")
    print("Offline demo: no real API calls / 离线演示：未调用真实 API。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MedReady LLM Harness / 大模型调用管理")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Offline tests / 离线测试")
    modes.add_argument("--interactive", action="store_true", help="One live API call / 输入问题并调用真实 API")
    parser.add_argument("--json", action="store_true", help="Request JSON output / 请求 JSON 输出")
    args = parser.parse_args()
    if args.demo:
        return _demo()
    if not args.interactive:
        parser.print_help()
        return 0
    try:
        llm = MedReadyLLMHarness()
        print("Live DeepSeek API call; token usage may incur charges / 将调用真实 DeepSeek API，可能产生费用。")
        prompt = input("Your question / 请输入问题: ").strip()
        print(llm.chat("You are a helpful assistant. / 你是一个友好的助手。", prompt, json_mode=args.json))
        print("Token usage / Token 用量:", json.dumps(llm.usage_totals))
        return 0
    except (LLMError, ValueError) as exc:
        print(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled / 已取消。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
