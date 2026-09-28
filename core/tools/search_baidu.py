import os
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.harness_tool import BaseTool

SEARCH_URL = 'https://qianfan.baidubce.com/v2/ai_search/web_search'


def build_queries(hospital: str, service: str) -> list[str]:
    related = '胃肠镜' if service in ('胃镜', '无痛胃镜', '肠镜', '无痛肠镜') else service
    department = '内镜中心' if related == '胃肠镜' else service
    return [
        f'{hospital} {department} 检查须知',
        f'{hospital} {related} 预约流程 携带',
        f'{hospital} 就医须知 预约挂号',
    ]


GUIDANCE = ('须知', '指南', '攻略', '预约流程', '检查流程', '携带', '准备', '注意事项')


def wrong_procedure(title: str, service: str) -> bool:
    if service in ('胃镜', '无痛胃镜', '肠镜', '无痛肠镜'):
        return any(word in title for word in ('CT', '核磁', '磁共振', '牙科', '产科')) and not any(
            word in title for word in ('胃镜', '肠镜', '内镜'))
    return False


def content_score(title: str, text: str, service: str) -> float:
    """Prefer instructions over biographies, directories, and repeated site footers."""
    score = 0.0
    if wrong_procedure(title, service):
        return -100
    if service in ('胃镜', '无痛胃镜') and '肠道准备' in title:
        score -= 25
    if any(word in title for word in GUIDANCE):
        score += 12
    if service in title or (service in ('胃镜', '肠镜', '无痛胃镜', '无痛肠镜') and '胃肠镜' in title):
        score += 6
    if service in text:
        score += 2
    score += min(sum(word in text for word in ('携带', '预约流程', '检查前', '取号', '报到', '就诊预约方式')), 3) * 2
    if any(word in text for word in ('详细介绍', '擅长', '毕业于')) and not any(word in title for word in GUIDANCE):
        score -= 12
    if any(word in title for word in ('简介', '专家', '医生', '医师', '论文', '科普')):
        score -= 6
    if any(word in title for word in ('国庆', '春节', '劳动节')):
        score -= 20
    return score


def rank_results(items: list[dict], hospital: str, service: str) -> list[dict]:
    seen = set()
    seen_content = set()
    ranked = []
    hospital_short = hospital.replace('医院', '').replace('附属', '')
    hospital_hosts = {urlparse(str(s.get('url') or '')).hostname for s in items
                      if hospital in str(s.get('website') or '')}
    for item in items:
        url = str(item.get('url') or '')
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or url in seen:
            continue
        seen.add(url)
        title = str(item.get('title') or '')
        identity = title + ' ' + str(item.get('website') or '')
        if hospital not in identity and hospital_short not in identity and parsed.hostname not in hospital_hosts:
            continue
        excerpt = BeautifulSoup(str(item.get('content') or ''), 'html.parser').get_text(' ', strip=True)
        fingerprint = (parsed.hostname, re.sub(r'\s+', '', excerpt)[:100])
        if len(excerpt) > 40 and fingerprint in seen_content:
            continue
        seen_content.add(fingerprint)
        haystack = title + ' ' + excerpt + ' ' + str(item.get('website') or '')
        score = content_score(title, excerpt, service)
        if hospital in haystack:
            score += 4
        elif hospital_short and hospital_short in haystack:
            score += 2
        if service in haystack:
            score += 2
        publisher = str(item.get('website') or '')
        if hospital in publisher:
            score += 3
        score += min(float(item.get('rerank_score') or 0), 1)
        ranked.append({'title': title, 'url': url, 'snippet': excerpt[:2000],
                       'publisher': str(item.get('website') or ''), 'date': str(item.get('date') or ''),
                       'relevance_score': round(score, 2)})
    return sorted(ranked, key=lambda x: x['relevance_score'], reverse=True)


class BaiduSearchTool(BaseTool):
    name = 'search_baidu'
    timeout = 40

    def __init__(self, api_key: str | None = None, session=None):
        key = (api_key or os.getenv('BAIDU_SEARCH_API_KEY', '')).strip()
        # The configuration may contain the raw key or a copied "Bearer <key>" value.
        self.api_key = key[7:].strip() if key.lower().startswith('bearer ') else key
        self.session = session or requests.Session()

    def run(self, params: dict) -> list[dict]:
        if not self.api_key:
            raise RuntimeError('BAIDU_SEARCH_API_KEY is not configured')
        hospital = params['hospital'].strip()
        service = params['service'].strip()
        collected = []
        # Open-web queries: no site allowlist or domain-suffix restriction.
        for query in build_queries(hospital, service):
            response = self.session.post(
                SEARCH_URL,
                headers={'X-Appbuilder-Authorization': f'Bearer {self.api_key}'},
                json={'messages': [{'role': 'user', 'content': query}],
                      'search_source': 'baidu_search_v2',
                      'resource_type_filter': [{'type': 'web', 'top_k': 10}]},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get('code'):
                raise RuntimeError(f"Baidu search error: {payload['code']}")
            collected.extend(payload.get('references') or [])
        return rank_results(collected, hospital, service)[:12]
