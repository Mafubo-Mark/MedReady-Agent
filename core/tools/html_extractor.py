import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.harness_tool import BaseTool


def public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 80, 443):
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (OSError, ValueError):
        return False


def extract_text(html: str | bytes) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form']):
        tag.decompose()
    main = soup.find('article') or soup.find('main') or soup.body or soup
    lines = [re.sub(r'\s+', ' ', s).strip() for s in main.stripped_strings]
    return '\n'.join(s for s in lines if len(s) > 1)[:6000]


def extract_title(html: str | bytes) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in (soup.find('h1'), soup.find('title')):
        if tag:
            title = re.sub(r'\s+', ' ', tag.get_text(' ', strip=True)).strip()
            if title and title != '分享到微信':
                return title[:180]
    return ''


def extract_guidance_links(html: str | bytes, base_url: str) -> list[dict]:
    """Discover bounded, same-site links; never restrict search by domain suffix."""
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for anchor in soup.select('a[href]'):
        label = anchor.get_text(' ', strip=True)
        if not any(word in label for word in ('须知', '指南', '预约', '检查流程')):
            continue
        if any(word in label for word in ('国庆', '春节', '劳动节', '清明', '端午', '元旦', '新闻', '会议', '放假')):
            continue
        url = urljoin(base_url, anchor['href']).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname != urlparse(base_url).hostname or url == base_url:
            continue
        if url.lower().endswith(('.pdf', '.jpg', '.png', '.zip')):
            continue
        score = 10 if any(word in label for word in ('须知', '指南')) else 4
        if any(word in label for word in ('内镜', '胃镜', '胃肠镜')):
            score += 8
        found[url] = {'title': label[:150], 'url': url, 'score': score}
    return sorted(found.values(), key=lambda x: x['score'], reverse=True)[:5]


class HtmlExtractorTool(BaseTool):
    name = 'html_extractor'
    timeout = 12

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def run(self, params: dict) -> dict:
        url = params['url']
        for _ in range(4):
            if not public_http_url(url):
                raise ValueError('URL is not a public HTTP website')
            response = self.session.get(url, timeout=8, allow_redirects=False,
                                        headers={'User-Agent': 'MedReady/0.1 (visit preparation)'})
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers['Location'])
                continue
            response.raise_for_status()
            if 'text/html' not in response.headers.get('Content-Type', '').lower():
                raise ValueError('Page is not HTML')
            if len(response.content) > 2_000_000:
                raise ValueError('Page is too large')
            # Let BeautifulSoup detect the charset from the HTML bytes. Some hospital
            # sites serve UTF-8 pages with an ISO-8859-1 HTTP fallback encoding.
            return {'url': url, 'text': extract_text(response.content),
                    'title': extract_title(response.content),
                    'links': extract_guidance_links(response.content, url)}
        raise ValueError('Too many redirects')
