import os
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

load_dotenv()
from core.harness_runtime import MedReadyAgentRuntime
app = FastAPI(title='MedReady Agent API', version='0.2.0')
origins = [x.strip() for x in os.getenv('ALLOWED_ORIGINS', 'http://localhost:8501,http://127.0.0.1:8501').split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=['POST', 'GET'], allow_headers=['Content-Type'])
runtime = MedReadyAgentRuntime()
visits: dict[str, deque] = defaultdict(deque)


class PrepareRequest(BaseModel):
    hospital: str = Field(min_length=2, max_length=100)
    service: str = Field(min_length=2, max_length=100)


@app.middleware('http')
async def rate_limit(request: Request, call_next):
    if request.url.path != '/api/v1/prepare':
        return await call_next(request)
    client = request.client.host if request.client else 'unknown'
    now = time.monotonic()
    bucket = visits[client]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= 10:
        return JSONResponse(status_code=429, content={'code': 429, 'msg': '请求过于频繁，请一分钟后重试。', 'data': {}})
    bucket.append(now)
    return await call_next(request)


@app.get('/health')
def health():
    return {'status': 'ok', 'version': app.version}


@app.post('/api/v1/prepare')
def prepare(body: PrepareRequest):
    data = runtime.run(body.hospital, body.service)
    return {'code': 200, 'msg': 'ok', 'data': data}


@app.exception_handler(Exception)
async def error_handler(_request: Request, _exc: Exception):
    return JSONResponse(status_code=500, content={'code': 500, 'msg': '服务暂不可用，请稍后重试。', 'data': {}})
