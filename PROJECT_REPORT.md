# MedReady Agent MVP Project Report

## What was built

The local MVP accepts a hospital name and a service or examination. It searches Baidu AI Search across the open web without a domain allowlist, inspects up to eight relevant pages and selects up to three sources, asks DeepSeek for structured logistics, validates source excerpts, and displays results in Streamlit. If search or model access fails, it returns an unverified message instead of inventing hospital requirements.

The supplied nine-day plan is broader than this MVP. This build does not include Docker, cloud deployment, a GitHub repository, or a clinical/legal approval process.

The document's `deepseek-chat` model name was replaced with the current `deepseek-flash` API model. Its old model name was retired in 2026. The default can be changed with `DEEPSEEK_MODEL`.

## File structure and responsibilities

| Path | Responsibility |
| --- | --- |
| `frontend/app.py` | Streamlit form, result cards, source links, disclaimer |
| `api/main.py` | FastAPI request validation, local CORS, rate limit, health endpoint |
| `core/harness_runtime.py` | Search/extract/model/validate sequence and fallback |
| `core/retrieval.py` | Bounded page collection, relevant link discovery, source ranking and deduplication |
| `core/harness_llm.py` | DeepSeek JSON request, retries, token count |
| `core/harness_tool.py` | Tool base class, timeout wrapper, registry |
| `core/harness_state.py` | In-memory task status and context |
| `core/harness_compliance.py` | Blocks explicit diagnosis and medication requests; injects disclaimer |
| `core/harness_logger.py` | Console logging and optional rotating file logs |
| `core/tools/search_baidu.py` | Unrestricted Baidu Qianfan web search and relevance ranking |
| `core/tools/html_extractor.py` | HTML text extraction and private-network URL blocking |
| `utils/json_validator.py` | Result shape, excerpt checks, unsupported-field fallback |
| `utils/cache.py` | One-day local cache for results with cited fields |
| `tests/test_modules.py` | Unit and mocked workflow tests |
| `tests/test_api.py` | API contract, validation, health, and rate limit tests |
| `.env.example` | Configuration template without credentials |
| `requirements.txt` | Runtime Python dependencies |
| `requirements-dev.txt` | Test dependencies, including the HTTP client needed by TestClient |
| `start.command` | One-click macOS launcher for API and frontend |

## How to use it

1. Revoke the credentials visible in the original document and issue new keys.
2. Run the setup commands in `README.md` from this folder.
3. Put the new `BAIDU_SEARCH_API_KEY` and `DEEPSEEK_API_KEY` in the ignored `.env` file.
4. On macOS, double-click `start.command`. It installs packages on first launch, starts both services, and opens the browser. The manual commands are also in `README.md`.
5. Open the Streamlit page, enter the hospital and project, and inspect each field's excerpt and linked source. Confirm important details with the hospital.

## Verification performed

Commands: `.venv/bin/python -m pip install -r requirements-dev.txt`, then `.venv/bin/python -m unittest discover -s tests -q`

Result: **23 tests passed** on 2026-09-28 using the local Python 3.14.3 environment. Provider calls were mocked in tests. The following checks ran:

| Area | Verification |
| --- | --- |
| API | `/health`, response contract, invalid input, 10 calls/minute limiter |
| Search | Query generation contains no `site:` restriction; Baidu request has no site filter; hospital-wide appointment query; guide-over-biography ranking, duplicate suppression, unrelated Q&A filtering; `Bearer` prefix normalization; useful failure categories |
| HTML extraction | Removes navigation; rejects loopback and non-HTTP URLs; detects UTF-8 from HTML bytes even when the server omits its charset |
| LLM | Sends JSON response mode with the expected request shape |
| Validation | Keeps fields with source excerpts across HTML whitespace; replaces unsupported claims with unknown; quotes explicit hospital appointment lines when the model misses them; preserves campus scope; rejects blood-test fasting as endoscopy preparation |
| Runtime | Completes mocked end-to-end flow; prefers the hospital's own source for the Binzhou gastroenterology case; returns unverified fallback on search failure |
| Compliance | Blocks explicit medication requests and medication instructions in model output while allowing a procedure query |
| State and cache | Status and context changes; cache expiry |
| Frontend | Streamlit page loads without exceptions, exposes both inputs, displays supported fields, and groups missing fields |

The API was exercised with FastAPI's in-process `TestClient`, and the UI with Streamlit `AppTest`. Public hospital pages were fetched successfully with Chinese text decoding verified.

Live Baidu and DeepSeek requests were also completed using the local configuration. The original Beijing query ranked physician biographies and duplicate snippets too highly. Updated search terminology and source ranking found the hospital's actual visit guide, and the workflow returned source-backed logistics. Search remains open web without a domain suffix allowlist.

Final browser verification used a separate local instance at `http://127.0.0.1:8766` (API port 8765), because the existing 8000/8501 processes could not be stopped from this session. The normal launcher still uses 8000/8501. The browser query is **北京协和医院 · 胃镜**. Its cited guide concerns **国际医疗部 · 东单院区**, so this scope is displayed alongside the fields. It does not establish requirements for all hospital patients.

The final browser result displayed three supported sections: materials, appointment process, and location, each with the international department / Dongdan campus scope. Fasting, fees, insurance, and other precautions remained unverified and were grouped into one notice. The source link and original excerpts were visible.

The browser check also caught an unrelated blood-test fasting sentence in the fasting section. A validator guard and regression test now reject that mismatch, and the cache version was advanced so older results are not reused.

## Known limits and next work

- A matching excerpt does not prove that a website is authentic or that every word in a model paraphrase is correct. A production release needs stronger publisher verification and human review of representative results.
- Search snippets can be used if the full page is inaccessible; they may omit context. A future version should label snippet-only evidence per field.
- The simple compliance patterns do not cover every medical advice request. This is an MVP guardrail, not a medical safety certification.
- In-memory rate limits are single-process only. Public deployment needs a shared limiter, authentication, monitoring, and a privacy review.
- Live verification covers the Beijing gastroscopy query, not every hospital or procedure. Search results, website accessibility, and model output can change; missing details remain explicitly unverified.

Provider references: [Baidu search API](https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy); [DeepSeek chat completions](https://api-docs.deepseek.com/api/create-chat-completion/); [DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode/).
