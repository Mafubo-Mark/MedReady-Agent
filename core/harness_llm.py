import os
import time

import requests

from core.harness_logger import get_logger


class MedReadyLLMHarness:
    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key or os.getenv('DEEPSEEK_API_KEY', '')
        self.base_url = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        self.model = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
        self.session = session or requests.Session()

    def chat(self, system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
        if not self.api_key:
            raise RuntimeError('DEEPSEEK_API_KEY is not configured')
        payload = {'model': self.model,
                   'messages': [{'role': 'system', 'content': system_prompt},
                                {'role': 'user', 'content': user_prompt}],
                   'max_tokens': 2200, 'temperature': 0,
                   'thinking': {'type': 'disabled'}}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.post(
                    f'{self.base_url}/chat/completions',
                    headers={'Authorization': f'Bearer {self.api_key}'},
                    json=payload, timeout=30)
                response.raise_for_status()
                data = response.json()
                choice = data['choices'][0]
                if choice.get('finish_reason') != 'stop':
                    raise ValueError('Model response was incomplete')
                text = choice['message'].get('content') or ''
                if not text.strip():
                    raise ValueError('Model response was empty')
                usage = data.get('usage') or {}
                get_logger(__name__).info('LLM tokens=%s', usage.get('total_tokens', 'unknown'))
                return text
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise RuntimeError(f'LLM call failed: {type(last_error).__name__}') from last_error
