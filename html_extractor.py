"""MedReady Phase 2-2: public webpage text extraction / 阶段 2-2：公开网页正文提取。

Implements BaseTool.run({"url": ...}) and returns raw data for the harness:
    {"text": str, "title": str, "url": str, "original_url": str,
     "char_count": int, "original_char_count": int, "truncated": bool}
实现 BaseTool.run，返回正文、标题、最终网址、原网址、字符数及截断标记。

Use execute() or ToolHarness.call() to receive the standard outer envelope.
extract(url) is a convenience returning an empty string on failure. Runtime
updates TaskState and assembles sources. Propagate the truncated flag when
building model context; omitted text must not be treated as complete evidence.
通过 execute() 或 ToolHarness.call() 获取统一外层结果。
extract(url) 在失败时返回空字符串。Runtime 负责更新状态、整理来源及保留截断标记。

Needs your existing core/harness_tool.py, core/harness_logger.py and
beautifulsoup4==4.12.3. Downloading uses Python's standard HTTP library. No API
key, browser, JavaScript execution, login, OCR, or PDF parsing is involved.
依赖已有工具 Harness、日志模块及 BeautifulSoup4。下载使用标准库，无需 API 密钥。
不执行 JavaScript，不登录，也不提供 OCR 或 PDF 解析。

Only public HTTP(S) URLs on ports 80/443 are accepted. DNS answers are checked
and the connection is pinned to a checked IP while HTTPS verifies the original
hostname. Redirects are checked individually. Environment proxies are not used.
仅接受公网 HTTP(S) 地址及 80/443 端口；校验 DNS 后固定连接目标 IP，HTTPS 仍验证原域名。
逐次检查重定向，不使用环境代理。网站是否属于医院官网仍需搜索/来源校验层确认。

The caller deadline defaults to 10 seconds (BaseTool). Network and body-read
checks also use a deadline. DNS/parsing cannot be forcibly cancelled by threads;
the existing harness limits concurrent workers. Body size is limited to 2 MiB
both before and after decompression. Up to five redirects are followed.
默认调用方等待期限为 10 秒，并对网络读取设置时间预算。线程不能强制取消 DNS 或解析，
由已有 Harness 限制并发。压缩前后响应体都限制为 2 MiB，最多跟随五次重定向。

Main-content selection and boilerplate removal are heuristics, not a guarantee
of perfect extraction. Text is capped at 4,000 characters, not tokens, and is
untrusted reference material for later checks, not medical advice.
正文定位和噪声清理属于启发式处理，不保证完美提取。每页最多 4,000 个字符（不是 token），
输出是待核实参考文本，仍需后续校验，不构成医疗建议。

From your project root / 在项目根目录运行：
    python3 -m core.tools.html_extractor --demo
    python3 -m core.tools.html_extractor --interactive
    python3 -m core.tools.html_extractor --url "https://example.com/article"
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import ssl
import zlib
from time import perf_counter
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from core.harness_tool import BaseTool, ToolError, ToolHarness

__all__ = ["HtmlExtractorTool"]
_MAX_BODY = 2 * 1024 * 1024
_NOISE = re.compile(
    r"(?:^|[\s_-])(?:nav|navigation|menu|breadcrumb|sidebar|footer|ad|ads|"
    r"advertisement|advertising|share|social|toolbar|related)(?:$|[\s_-])", re.I
)


def _public_target(url: str) -> tuple[str, str, int, str, list[str]]:
    """Validate a URL and resolve public addresses / 校验网址并解析公网 IP。"""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("Enter a webpage URL / 请输入网页链接。")
    url = url.strip()
    if len(url) > 4096 or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ToolError("Invalid webpage URL / 网页链接格式无效。")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or "%" in host:
            raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        if port not in (80, 443):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ToolError("Use a public http:// or https:// URL without credentials / 请使用不含账号密码的公网 HTTP(S) 链接。") from None

    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = {record[4][0] for record in records}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ToolError("Local/private network URLs are not allowed / 不允许访问本地或私有网络地址。")
    # Prefer IPv4 when both families are returned / 同时存在时优先 IPv4。
    ips = sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
    netloc = f"[{host}]" if ":" in host else host
    if port != (443 if parsed.scheme.lower() == "https" else 80):
        netloc += f":{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    return canonical, host, port, path + ("?" + query if query else ""), ips


class _PinnedConnection(http.client.HTTPConnection):
    """Connect to a validated IP with the original Host/SNI / 连接已校验 IP 并保留原 Host/SNI。"""

    def __init__(self, host: str, port: int, ip: str, secure: bool, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._ip = ip
        self._secure = secure

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self._secure else raw
        except BaseException:
            raw.close()
            raise


class HtmlExtractorTool(BaseTool):
    """Download and clean HTML / 下载并清理 HTML 正文。"""

    def __init__(self, *, name: str = "html_extractor", timeout: float = 10.0, max_chars: int = 4000):
        super().__init__(name=name, timeout=timeout)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 4000:
            raise ValueError("max_chars must be an integer from 1 to 4000 / 字符上限必须为 1-4000 的整数。")
        self.max_chars = max_chars

    def _download(self, original_url: str) -> tuple[bytes, str, str | None]:
        deadline = perf_counter() + self.timeout
        url, visited = original_url, set()

        def remaining() -> float:
            value = deadline - perf_counter()
            if value <= 0:
                raise ToolError("Webpage download timed out / 网页下载超时。")
            return value

        for redirect in range(6):
            remaining()
            canonical, host, port, target, ips = _public_target(url)
            if canonical in visited:
                raise ToolError("Webpage redirect loop / 网页重定向循环。")
            visited.add(canonical)
            connection = _PinnedConnection(host, port, ips[0], canonical.startswith("https:"), remaining())
            try:
                connection.request("GET", target, headers={
                    "User-Agent": "MedReady-Agent/0.1 (public-page text extraction)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                connection.sock.settimeout(remaining())
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or redirect == 5:
                        raise ToolError("Invalid or excessive webpage redirects / 网页重定向无效或次数过多。")
                    url = urljoin(canonical, location)
                    continue
                if response.status != 200:
                    raise ToolError(f"Webpage HTTP error {response.status} / 网页 HTTP 错误 {response.status}。")
                content_type = response.getheader("Content-Type", "")
                mime = content_type.split(";", 1)[0].strip().lower()
                if mime and mime not in ("text/html", "application/xhtml+xml"):
                    raise ToolError("This tool supports HTML pages only / 本工具仅支持 HTML 网页，不支持 PDF、图片等文件。")
                encoding = response.headers.get_content_charset()
                compression = response.getheader("Content-Encoding", "identity").lower().strip()
                if compression not in ("", "identity", "gzip", "deflate"):
                    raise ToolError("Unsupported webpage compression / 暂不支持此网页压缩格式。")
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if compression == "gzip" else zlib.MAX_WBITS) if compression in ("gzip", "deflate") else None
                body, received = bytearray(), 0
                while True:
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining())
                    remaining()
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _MAX_BODY:
                        raise ToolError("Webpage exceeds the 2 MiB download limit / 网页超过 2 MiB 下载限制。")
                    body.extend(decoder.decompress(chunk, _MAX_BODY - len(body) + 1) if decoder else chunk)
                    if len(body) > _MAX_BODY:
                        raise ToolError("Decoded webpage exceeds the size limit / 解压后的网页超过大小限制。")
                if response.length not in (None, 0):
                    raise ToolError("Webpage download was incomplete / 网页下载不完整。")
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise ToolError("Invalid compressed webpage / 压缩网页不完整或格式不支持。")
                if not body:
                    raise ToolError("The webpage response is empty / 网页响应为空。")
                if not mime and not re.search(br"<(?:!doctype\s+html|html|head|body|main|article)\b", bytes(body[:2048]), re.I):
                    raise ToolError("Response does not appear to be HTML / 响应内容未识别为 HTML。")
                remaining()
                return bytes(body), canonical, encoding
            finally:
                connection.close()
        raise ToolError("Too many redirects / 重定向次数过多。")

    def parse_html(self, html: bytes | str, *, url: str = "", encoding: str | None = None) -> dict:
        """Clean supplied HTML without networking / 清理传入 HTML，不发起网络请求。

        Useful for local tests. URL metadata is supplied by the caller here;
        run() obtains it from the checked downloader. This does not certify sources.
        用于本地测试。此方法的网址元数据由调用方提供；run() 使用下载器校验后的网址。
        本方法不认证信息来源。
        """
        if not isinstance(html, (bytes, str)) or not html.strip():
            raise ToolError("HTML content is empty or invalid / HTML 内容为空或无效。")
        if len(html) > _MAX_BODY:
            raise ToolError("HTML exceeds the parsing limit / HTML 超过解析大小限制。")
        if encoding and encoding.casefold() in ("gb2312", "gbk"):
            encoding = "gb18030"
        soup = BeautifulSoup(html, "html.parser", from_encoding=encoding if isinstance(html, bytes) else None)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for tag in soup.find_all([
            "script", "style", "noscript", "template", "svg", "canvas", "iframe",
            "head", "object", "embed", "nav", "footer", "aside", "form", "button", "input",
        ]):
            if tag.parent is not None:
                tag.decompose()
        for tag in list(soup.find_all(True)):
            if tag.parent is None or tag.attrs is None:
                continue
            signals = " ".join([str(tag.get("id", "")), " ".join(tag.get("class", []))])
            hidden = tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true"
            hidden = hidden or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(tag.get("style", "")), re.I))
            role = str(tag.get("role", "")).lower()
            if hidden or role in ("navigation", "banner", "contentinfo") or _NOISE.search(signals):
                tag.decompose()

        candidates = soup.select("article, main, [role=main], .article-content, .article_content, .TRS_Editor, #zoom, #content, .content")

        def score(tag) -> int:
            total = len(tag.get_text(" ", strip=True))
            link_text = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
            return total - 2 * link_text

        usable = [tag for tag in candidates if tag.get_text(strip=True)]
        root = max(usable, key=score) if usable else (soup.body or soup)
        for tag in root.find_all("br"):
            tag.replace_with("\n")
        for tag in root.find_all(["p", "div", "section", "article", "header", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "ul", "ol", "blockquote"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for tag in root.find_all(["td", "th"]):
            tag.insert_after(" | ")
        # Keep inline text joined, and preserve boundaries between text blocks.
        # 保持行内文字连贯，同时保留段落和表格行的边界。
        lines = [" ".join(line.split()).strip(" |") for line in root.get_text().splitlines()]
        text = "\n".join(line for line in lines if line)
        if not text:
            raise ToolError("No usable page text found; the page may require JavaScript / 未找到可用正文，页面可能需要 JavaScript。")
        original_count = len(text)
        truncated = original_count > self.max_chars
        if truncated:
            text = text[:self.max_chars]
            boundary = text.rfind("\n")
            if boundary >= self.max_chars * 0.75:
                text = text[:boundary]
            text = text.rstrip()
        return {
            "text": text, "title": title, "url": url,
            "char_count": len(text), "original_char_count": original_count,
            "truncated": truncated,
        }

    def run(self, params: dict) -> dict:
        """Download and extract a page / 下载并提取网页。Use execute()/harness.call()."""
        url = params.get("url")
        try:
            body, final_url, encoding = self._download(url)
            result = self.parse_html(body, url=final_url, encoding=encoding)
        except ToolError:
            raise
        except (TimeoutError, socket.timeout):
            raise ToolError("Webpage request timed out / 网页请求超时。") from None
        except (OSError, http.client.HTTPException, zlib.error):
            raise ToolError("Could not download the webpage; check connectivity and website availability / 无法下载网页，请检查网络及网站可用性。") from None
        result["original_url"] = url
        return result

    def extract(self, url: str) -> str:
        """Return text or an empty string on failure / 返回正文，失败时返回空字符串。"""
        result = self.execute({"url": url})
        return result["data"]["text"] if result["success"] else ""


def _demo() -> int:
    """Offline harness demo / 离线 Harness 演示。"""
    from unittest.mock import patch

    html = """<!doctype html><html><head><meta charset="utf-8"><title>Demo guide / 演示指南</title>
    <style>.x { color: red }</style></head><body><nav>NAVIGATION_NOISE</nav>
    <main><article><header><h1>Visit guide / 就诊指南</h1></header>
    <p>Demonstration <strong>content</strong> only / 仅为演示内容。</p>
    <ul><li>Section A / 第一部分</li><li>Section B / 第二部分</li></ul>
    <div class="advertisement">ADVERTISEMENT_NOISE</div>
    <script>SECRET_SCRIPT_NOISE</script></article></main><footer>FOOTER_NOISE</footer></body></html>"""
    tool = HtmlExtractorTool()
    harness = ToolHarness()
    harness.register(tool)
    with patch.object(tool, "_download", return_value=(html.encode(), "https://hospital.example/guide", "utf-8")):
        result = harness.call("html_extractor", {"url": "https://hospital.example/guide"})
    assert result["success"]
    assert "NOISE" not in result["data"]["text"]
    assert "Demonstration content only" in result["data"]["text"]
    assert "第一部分" in result["data"]["text"]
    bounded = tool.parse_html("<article><p>" + "字" * 4500 + "</p></article>")
    assert bounded["char_count"] == 4000 and bounded["truncated"]
    with patch.object(tool, "_download", side_effect=ToolError("Demo failure / 演示失败")):
        assert tool.extract("https://hospital.example/guide") == ""
    print("OFFLINE DEMO / 离线演示：no real page downloaded / 未下载真实网页。")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("PASS / 通过：cleanup, Unicode, truncation, harness integration and failures / 清理、中文、截断、Harness 集成与失败处理。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MedReady HTML extractor / 网页正文提取")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Offline test / 离线测试")
    modes.add_argument("--interactive", action="store_true", help="Prompt for one webpage URL / 输入网页链接")
    modes.add_argument("--url", help="Extract one public HTML page / 提取公开 HTML 网页")
    args = parser.parse_args()
    if args.demo:
        return _demo()
    if not args.interactive and args.url is None:
        parser.print_help()
        return 0
    try:
        url = input("Hospital article URL / 医院具体文章的网页链接: ").strip() if args.interactive else args.url
        result = HtmlExtractorTool().execute({"url": url})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["success"] else 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled / 已取消。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
