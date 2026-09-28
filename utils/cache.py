import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[1] / 'cache'


def make_key(hospital: str, service: str) -> str:
    normalized = f'guidance-v3|{hospital.strip().casefold()}|{service.strip().casefold()}'
    return hashlib.sha256(normalized.encode()).hexdigest()


def get_cache(key: str) -> dict | None:
    path = CACHE_DIR / f'{key}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['expires'] > time.time():
            return data['value']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def set_cache(key: str, value: dict, ttl: int = 86400):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f'{key}.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'expires': time.time() + ttl, 'value': value}, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
