"""Select useful documents and follow a bounded set of hospital guidance links."""
import hashlib
import re
from urllib.parse import urlparse

from core.tools.search_baidu import content_score, GUIDANCE, wrong_procedure


def collect_sources(registry, results: list[dict], hospital: str, service: str) -> list[dict]:
    hospital_hosts = {urlparse(s['url']).hostname for s in results if hospital in s.get('publisher', '')}
    queue = []
    for item in results:
        # An author's employer in a generic medical Q&A is not hospital guidance.
        institution_match = hospital in (item['title'] + item.get('publisher', ''))
        if not institution_match and urlparse(item['url']).hostname not in hospital_hosts:
            continue
        if wrong_procedure(item['title'], service):
            continue
        queue.append({**item, 'depth': 0, 'priority': item.get('relevance_score', 0)})
    visited, documents, seen_text = set(), [], set()
    while queue and len(visited) < 8:
        queue.sort(key=lambda s: s['priority'], reverse=True)
        item = queue.pop(0)
        if item['url'] in visited:
            continue
        visited.add(item['url'])
        extraction = registry.call('html_extractor', {'url': item['url']})
        page = extraction['data'] if extraction['success'] else {}
        final_url = page.get('url', item['url'])
        body = page.get('text', '')
        title = page.get('title') or item['title']
        if any(word in body[:500] for word in ('环境异常', '完成验证后', '访问验证', '验证码')) and len(body) < 1500:
            body = ''
            final_url = item['url']
        title = title.split(' - ')[0]
        if not any(word in title for word in GUIDANCE) and any(word in item['title'] for word in GUIDANCE):
            title = item['title']
        if item['depth'] < 3:
            for link in page.get('links', [])[:3]:
                if link['url'] not in visited and not wrong_procedure(link['title'], service):
                    queue.append({'title': link['title'], 'url': link['url'], 'snippet': '',
                                  'publisher': item.get('publisher', ''), 'date': '',
                                  'depth': item['depth'] + 1,
                                  'priority': 16 + link.get('score', 0) + content_score(link['title'], '', service)})
        text = body if len(body) >= 100 else item.get('snippet', '')
        if not text:
            continue
        fingerprint = hashlib.sha256(re.sub(r'\s+', '', text).encode()).hexdigest()
        if fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        score = content_score(title, text, service)
        if any(word in text for word in ('出诊信息', '擅长', '毕业于')) and not any(word in title for word in GUIDANCE):
            continue
        if score < 4:
            continue
        documents.append({**item, 'url': final_url, 'title': title, 'text': text,
                          'evidence_type': 'page' if len(body) >= 100 else 'search snippet',
                          'content_score': score})
    documents.sort(key=lambda s: s['content_score'], reverse=True)
    return documents[:3]
