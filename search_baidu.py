"""MedReady Phase 2-1: Baidu Qianfan search / 阶段 2-1：百度千帆搜索。

Provider reference / 接口依据 (checked 2026-09-14):
https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5

Configuration / 配置：config/.env
    BAIDU_SEARCH_API_KEY=<your key / 你的密钥>
    BAIDU_SEARCH_BASE_URL=https://qianfan.baidubce.com/v2/ai_search/web_search
    BAIDU_SEARCH_ALLOWED_SITES=gov.cn

Add verified hospital domains to ALLOWED_SITES, comma-separated, or pass
site_list in each call. The default is government sites ONLY. A hostname
allowlist does not itself prove that a site is an official hospital source.
将核实过的医院官网域名加入 ALLOWED_SITES，用逗号分隔；也可每次传入 site_list。
默认只查 gov.cn。域名白名单本身不证明网站属于医院官网，须由维护者核实。

Optional / 可选：BAIDU_SEARCH_AUTH_HEADER=Authorization
Use X-Appbuilder-Authorization only if your console's key instructions require
it. The key value must not include 'Bearer '. Existing process environment
variables override .env, without modifying global environment variables.
如控制台要求 AppBuilder 认证，可改为 X-Appbuilder-Authorization。
密钥值不含 Bearer 前缀。进程环境变量优先于 .env，不修改全局环境。

Usage / 用法：
    tool = BaiduSearchTool()
    harness.register(tool)
    result = harness.call("baidu_search", {
        "query": "北京协和医院 胃镜 检查须知",
        "site_list": ["gov.cn"],
    })

Each run performs ONE request and returns up to five filtered, unique results.
build_queries(hospital, service) creates three strategies; the runtime calls
them separately. This uses Baidu's structured site filter, not Bing syntax.
每次 run 发起一次请求，返回最多五条过滤、去重后的结果。
build_queries 生成三组策略，由 Runtime 分别调用。使用百度站点过滤参数。

Use execute()/harness.call() for success/error details. search() is a legacy
convenience returning [] on both errors and no matches; do not use it when
the runtime must distinguish those cases. Results are untrusted reference
text, not medical advice or a complete checklist. State updates and final
output screening remain the runtime's responsibility.
execute()/harness.call() 保留成功与错误信息。search() 在失败或无结果时都返回 []，
需要区分二者时不要使用它。结果是待核实的参考文本，不是医疗建议或完整清单。
任务状态更新及最终输出审核仍由 Runtime 负责。

CLI / 命令行：
    python3 -m core.tools.search_baidu --demo         # Offline / 离线
    python3 -m core.tools.search_baidu --interactive  # Live / 真实请求

Uses requests and python-dotenv from your existing requirements.txt. Live CLI
also uses your compliance module and rule file. No automatic retries or
unrestricted-search fallback. API errors never expose raw response content.
使用已有依赖 requests、python-dotenv。真实请求命令还使用合规模块及规则文件。
不自动重试、不放宽站点限制，错误提示不泄露接口响应原文。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import dotenv_values

from core.harness_tool import BaseTool, ToolError, ToolHarness

__all__ = ["BaiduSearchTool", "build_queries"]

_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
_ENV_PATH = Path(__file__).resolve().parents[2] / "config" / ".env"
_MAX_RESPONSE_BYTES = 2_000_000


def _query(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Enter a non-empty query / 请输入非空搜索词。")
    text = " ".join(text.split())
    # Conservative length check: non-ASCII counts as two units.
    # 保守计数：非 ASCII 字符按两个单位计算，避免服务端静默截断。
    if sum(1 if ord(char) < 128 else 2 for char in text) > 72:
        raise ValueError("Query is too long; shorten the hospital/service name / 搜索词过长，请缩短医院或项目名称。")
    return text


def build_queries(hospital: str, service: str) -> list[str]:
    """Build three Chinese hospital-search strategies / 生成三组医院搜索策略。"""
    hospital, service = _query(hospital), _query(service)
    return [
        _query(f"{hospital} {service} 检查须知 注意事项"),
        _query(f"{hospital} {service} 就诊指南 预约流程"),
        _query(f"{hospital} 门诊 就诊须知 携带材料"),
    ]


def _domain(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid site domain / 站点域名无效。")
    try:
        domain = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Invalid site domain / 站点域名无效。") from None
    if len(domain) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", domain
    ):
        raise ValueError("Use domains only, without URLs or wildcards / 仅填写域名，不带网址路径或通配符。")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("Use a website domain, not an IP address / 请使用网站域名，不使用 IP 地址。")


def _sites(values: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise ValueError("Provide 1-100 verified site domains / 请提供 1-100 个已核实的站点域名。")
    return list(dict.fromkeys(_domain(item) for item in values))


def _allowed_url(value: str, sites: list[str]) -> str | None:
    """Match hostname boundaries, not URL substrings / 按域名边界匹配，不做网址子串匹配。"""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        url = urlsplit(value.strip())
        if url.scheme.lower() not in ("http", "https") or url.username is not None or url.password is not None:
            return None
        if url.port not in (None, 80, 443):
            return None
        host = _domain(url.hostname or "")
        if not any(host == site or host.endswith("." + site) for site in sites):
            return None
        port = f":{url.port}" if url.port and url.port != (443 if url.scheme.lower() == "https" else 80) else ""
        return urlunsplit((url.scheme.lower(), host + port, url.path or "/", url.query, ""))
    except (ValueError, UnicodeError):
        return None


class BaiduSearchTool(BaseTool):
    """Search Baidu and return title/url/snippet records / 返回标题、链接及摘要。"""

    def __init__(
        self, *, name: str = "baidu_search", api_key: str | None = None,
        site_list: list[str] | None = None, timeout: float = 10.0,
        env_path: str | Path = _ENV_PATH,
    ) -> None:
        super().__init__(name=name, timeout=timeout)
        path = Path(env_path)
        config = dotenv_values(path, interpolate=False) if path.is_file() else {}

        def setting(key: str, default: str = "") -> str:
            return os.environ.get(key, config.get(key) or default)

        self._api_key = api_key if api_key is not None else setting("BAIDU_SEARCH_API_KEY")
        self._endpoint = setting("BAIDU_SEARCH_BASE_URL", _ENDPOINT).strip().rstrip("/")
        self._auth_header = setting("BAIDU_SEARCH_AUTH_HEADER", "Authorization").strip()
        configured_sites = setting("BAIDU_SEARCH_ALLOWED_SITES", "gov.cn").split(",")
        self._default_sites = _sites(site_list if site_list is not None else configured_sites)

    def _configuration_check(self) -> None:
        # Credentials only go to the known provider endpoint; redirects disabled.
        # 密钥仅发送至已知服务端点，且禁止跟随重定向。
        if self._endpoint != _ENDPOINT:
            raise ToolError("Check BAIDU_SEARCH_BASE_URL in config/.env / 请检查配置中的百度搜索地址。")
        if self._auth_header not in ("Authorization", "X-Appbuilder-Authorization"):
            raise ToolError("Unsupported BAIDU_SEARCH_AUTH_HEADER / 百度认证头配置无效。")
        key = self._api_key
        if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ToolError("Set BAIDU_SEARCH_API_KEY without Bearer or whitespace / 请配置百度密钥，不带 Bearer 或空白。")
        if "xxxx" in key.casefold() or "replace-with" in key.casefold():
            raise ToolError("Replace the placeholder Baidu key in config/.env / 请将百度密钥占位符替换为真实密钥。")

    def _request(self, payload: dict) -> dict:
        self._configuration_check()
        started = perf_counter()
        try:
            with requests.post(
                self._endpoint,
                headers={self._auth_header: f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=(min(3.0, self.timeout / 3), min(6.0, self.timeout * 2 / 3)),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in (401, 403):
                    raise ToolError("Baidu authentication/permission failed; check the key and service access / 百度认证或权限失败，请检查密钥及服务开通状态。")
                if response.status_code == 429:
                    raise ToolError("Baidu rate/quota limit reached; try later / 百度调用频率或额度受限，请稍后重试。")
                if response.status_code != 200:
                    raise ToolError(f"Baidu search HTTP error {response.status_code} / 百度搜索 HTTP 错误 {response.status_code}。")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if perf_counter() - started > self.timeout:
                        raise ToolError("Baidu response exceeded the time budget / 百度响应超过时间预算。")
                    content.extend(chunk)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise ToolError("Baidu response is too large / 百度响应数据过大。")
                try:
                    data = json.loads(content)
                except (ValueError, UnicodeError):
                    raise ToolError("Baidu returned invalid JSON / 百度返回的数据不是有效 JSON。") from None
        except requests.Timeout:
            raise ToolError("Baidu HTTP request timed out / 百度 HTTP 请求超时。") from None
        except requests.RequestException:
            raise ToolError("Could not connect to Baidu search / 无法连接百度搜索服务。") from None
        if not isinstance(data, dict):
            raise ToolError("Unexpected Baidu response structure / 百度响应结构不符合预期。")
        if data.get("code") not in (None, 0, "0", "") or data.get("error_code") not in (None, 0, "0", ""):
            raise ToolError("Baidu rejected the request; check credentials, access and quota / 百度拒绝请求，请检查密钥、权限及额度。")
        return data

    def run(self, params: dict) -> dict:
        """Execute one query / 执行一组搜索词。Call through the harness / 请通过 Harness 调用。"""
        try:
            query = _query(params.get("query"))
            supplied = params.get("site_list")
            sites = _sites(self._default_sites if supplied is None else supplied)
            top_k = params.get("top_k", 5)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 5:
                raise ValueError("top_k must be an integer from 1 to 5 / top_k 必须是 1-5 的整数。")
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        payload = {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "edition": "standard",
            "resource_type_filter": [{"type": "web", "top_k": top_k}],
            "search_filter": {"match": {"site": sites}},
        }
        data = self._request(payload)
        # Explicit [] means no results. Missing or malformed references means an
        # integration error, not a successful empty search.
        # 明确的 [] 表示无结果；缺失或格式异常属于接口错误。
        references = data.get("references")
        if not isinstance(references, list):
            raise ToolError("Baidu response has no valid references list / 百度响应缺少有效 references 列表。")
        results, seen = [], set()
        for item in references:
            if not isinstance(item, dict) or item.get("type", "web") != "web":
                continue
            url = _allowed_url(item.get("url"), sites)
            title = item.get("title")
            if not url or url in seen or not isinstance(title, str) or not title.strip():
                continue
            snippet = item.get("snippet") or item.get("content") or ""
            if not isinstance(snippet, str):
                snippet = ""
            seen.add(url)
            results.append({"title": title.strip(), "url": url, "snippet": snippet.strip()})
            if len(results) == top_k:
                break
        return {"results": results, "query": query, "site_list": sites, "provider": "baidu"}

    def search(self, query: str, site_list: list[str] | None = None) -> list[dict]:
        """Compatibility helper: [] on no matches OR failure / 兼容方法：无结果或失败均返回 []。"""
        result = self.execute({"query": query, "site_list": site_list})
        return result["data"]["results"] if result["success"] else []


def _demo() -> int:
    """Offline integration check; never sends a request / 离线集成检查，不发起真实请求。"""
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    example_response = requests.Response()
    example_response.status_code = 200
    example_response._content = json.dumps({"references": [
        {"title": "DEMO visit guide / 演示就诊指南", "url": "https://hospital.example/guide", "snippet": "Demonstration data only / 仅为演示数据"},
        {"title": "Duplicate / 重复", "url": "https://hospital.example/guide#top"},
        {"title": "Outside allowed domain / 非白名单", "url": "https://hospital.example.other.test/guide"},
    ]}).encode("utf-8")
    example_response._content_consumed = True
    with TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
        tool = BaiduSearchTool(api_key="offline-demo-token", site_list=["hospital.example"], env_path=Path(temporary) / "absent.env")
        manager = ToolHarness()
        manager.register(tool)
        with patch.object(requests, "post", return_value=example_response) as post:
            result = manager.call("baidu_search", {"query": "Example Hospital MRI"})
        assert post.call_args.kwargs["json"]["search_filter"]["match"]["site"] == ["hospital.example"]
        assert result["success"] and len(result["data"]["results"]) == 1
        with patch.object(requests, "post", side_effect=requests.Timeout):
            assert tool.search("MRI") == []
    print("OFFLINE DEMO / 离线演示：no real search or API key used / 未使用真实搜索或密钥。")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("PASS / 通过：harness integration, parsing, domain filtering, deduplication and error handling / Harness 集成、解析、域名过滤、去重和错误处理。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MedReady Baidu search / 百度搜索工具")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Offline test, no API calls / 离线测试")
    modes.add_argument("--interactive", action="store_true", help="Prompt for hospital/service and run three live searches / 输入医院和项目后执行三次真实搜索")
    modes.add_argument("--query", help="Run one live query / 执行一次真实搜索")
    parser.add_argument("--sites", nargs="+", help="Verified domains; overrides configured sites / 已核实域名，覆盖配置")
    args = parser.parse_args(argv)
    if args.demo:
        return _demo()
    if not args.interactive and args.query is None:
        parser.print_help()
        return 0
    try:
        from core.harness_compliance import check_input

        tool = BaiduSearchTool()
        sites = args.sites
        if args.interactive:
            hospital = input("Hospital name / 医院名称: ").strip()
            service = input("Examination or department / 检查项目或科室: ").strip()
            allowed, message = check_input(f"{hospital} {service}", language="both")
            if not allowed:
                print(message)
                return 1
            queries = build_queries(hospital, service)
            if sites is None:
                entered = input("Verified domains, comma-separated; Enter uses configured sites (default gov.cn) / 已核实域名，逗号分隔；回车使用配置（默认 gov.cn）: ").strip()
                sites = entered.split(",") if entered else None
        else:
            queries = [_query(args.query)]
        # Check EVERY query before any paid request / 真实请求前检查所有搜索词。
        for query in queries:
            allowed, message = check_input(query, language="both")
            if not allowed:
                print(message)
                return 1
        manager = ToolHarness()
        manager.register(tool)
        all_succeeded = True
        for i, query in enumerate(queries, 1):
            print(f"\nLive Baidu search / 真实百度搜索 {i}/{len(queries)}: {query}")
            result = manager.call("baidu_search", {"query": query, "site_list": sites})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["success"]:
                all_succeeded = False
                break  # Stop after an error; no automatic retry / 出错即停止，不自动重试。
        return 0 if all_succeeded else 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled / 已取消。")
        return 1
    except (ValueError, ToolError) as exc:
        print(str(exc))
        return 1
    except OSError:
        print("Could not read configuration; check config/.env and config/compliance_rules.json / 无法读取配置，请检查上述文件。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
