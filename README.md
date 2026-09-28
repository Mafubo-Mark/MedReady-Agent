# 诊前智备 MedReady Agent

A local MVP for finding hospital visit preparation and process information. It searches the open web through Baidu Qianfan, extracts relevant pages, asks DeepSeek for structured fields, and displays each supported field with a source excerpt. There is **no domain suffix allowlist**: hospital websites outside `.gov` are eligible.

This app does not diagnose conditions or recommend treatment. It does not verify that a source is genuinely operated by a hospital. Confirm important requirements directly with the hospital.

## Requirements

- Python 3.10 or newer
- Your own replacement Baidu Qianfan AppBuilder API key for AI web search
- Your own replacement DeepSeek API key

The credentials included in the original planning document were exposed. Revoke them. Do not paste them into this repository, tests, issues, or logs.

## Setup on macOS or Linux

### Easiest way on macOS

Copy `.env.example` to `.env`, add newly issued Baidu and DeepSeek keys, then double-click `start.command` in Finder. On its first run, it creates `.venv` and installs packages. Keep the Terminal window open while using the app; press Control+C there to stop it. If macOS blocks the script, right-click it and choose **Open**.

### Manual setup

From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with newly issued keys. `.env` is ignored by Git. Then start two terminals, activating the virtual environment in each:

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

```bash
python -m streamlit run frontend/app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`. API documentation is at `http://127.0.0.1:8000/docs`.

If no keys are configured, the app still opens but queries return a clear unverified fallback. The API never takes keys from a browser request.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use fake provider responses; they do not call Baidu or DeepSeek or require paid API access. See `PROJECT_REPORT.md` for per-module coverage and current limitations.

## API

`POST /api/v1/prepare` with JSON:

```json
{"hospital":"北京协和医院","service":"胃镜"}
```

The response is `{"code":200,"msg":"ok","data":{...}}`. `data.fields` contains seven sections. Each supported section has a `source_id` and an exact excerpt in `quote`. Unsupported sections say to confirm with the hospital. `data.sources` contains clickable URLs.

`GET /health` returns `{"status":"ok","version":"0.2.0"}`.

## Design notes

- Search is open web. It does not use `site:` or a `.gov` suffix restriction.
- Search result snippets are used when a page cannot be extracted. This is indicated in the source list but may be less reliable than a full hospital page.
- Source excerpts are matched to retrieved text before a field is shown. This is a mechanical check; it does not establish source authenticity or fully prove that the generated paraphrase is correct.
- Cached verified results expire after one day. `cache/` and `logs/` are ignored by Git.
- CORS defaults to the local Streamlit origins. IP rate limiting is in process and resets on server restart.
- This MVP is designed for local use. Public deployment needs stronger authentication, rate limiting, privacy controls, and manual source quality review.

Provider documentation: [Baidu AI Search](https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy) and [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/).

## September 28 retrieval update

Search now prioritizes examination instructions and appointment guides over doctor biographies, follows relevant links within a candidate website, and removes duplicate results. It can inspect up to eight pages and provide up to three sources to the model. Hospital sites using `.com.cn` and other suffixes remain eligible.

The frontend displays supported fields with quotations and campus/department scope. Unavailable fields are grouped into one notice. An international medical department's instructions do not establish the requirements for ordinary outpatient appointments. Blood-test fasting instructions are excluded from endoscopy fasting fields; medication advice is filtered out.

The macOS launcher watches backend code for changes. After this update, an already-running older launcher must be stopped once with Control+C and started again. Subsequent backend edits reload automatically. A fresh query may take around a minute depending on the search and model services; verified results are cached for one day.
