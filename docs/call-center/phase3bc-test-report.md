# Phase 3bc — Test Report

**Branch:** `feat/VOX-concierge-ui-timings`
**Run date:** initial 3bc verification

---

## 1. Unit tests — `tests/call_center/test_concierge.py`

```
$ pytest tests/call_center/test_concierge.py -q
================================= test session starts ==================================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
configfile: pyproject.toml
plugins: anyio-4.13.0, asyncio-1.3.0
collected 10 items

tests\call_center\test_concierge.py ..........                                    [100%]

================================== 10 passed in 0.24s ==================================
```

**Result:** 10 / 10 passed.

### 1.1 Existing 8 tests (Phase 3)

All Phase 3 tests still pass with the new `timings` key added to every
return path — confirms backward-compatible shape extension (additive
only; no existing key changed).

### 1.2 New 3c tests — `TestTimings`

| Test | Asserts |
|---|---|
| `test_happy_path_records_all_three_stages` | Returned `timings` dict contains `decompose_ms`, `retrieve_ms`, `render_ms`, `total_ms`. All values are `float >= 0`. `total_ms + 1.0 >= sum(stage_ms)` (the +1.0 tolerates rounding when `_ms()` rounds each stage independently). |
| `test_short_circuit_records_only_total` | Empty utterance returns `timings = {"total_ms": <float>}` — no stage keys present. |

## 2. UI smoke — `demo-hotel/voxtera-concierge.html`

Manual verification path (run by reviewer):

```powershell
cd demo-hotel
python serve.py
# then open http://localhost:8000/voxtera-concierge.html
```

Expected:

- Page renders in cream + Fraunces serif with the marketing nav, the
  "Concierge" link marked active and rendered in teal.
- The mode strip shows `[ Concierge ]  Find hotels & places — by what
  they offer.` with a Region dropdown on the right.
- Typing a request and pressing **Ask →** sends `POST /api/concierge`
  with `{utterance, region}` and renders:
  - User bubble (ink background, gold label, region in the label).
  - "Thinking…" system line that is replaced by the assistant bubble.
  - Assistant bubble in Instrument Serif italic with the answer text.
  - Up to 5 hotel cards with per-requirement evidence rows.
  - Collapsible *Debug · decomposition + timings* drawer with timing
    chips (`decompose 412ms`, `retrieve 184ms`, `render 901ms`,
    `total 1497ms` — actual values vary) and the raw decomposition
    JSON.

Network errors and HTTP non-200 responses render as an assistant bubble
labelled "Concierge · error".

## 3. Static / lint

```
get_errors demo-hotel/serve.py                  → No errors found
get_errors src/voxtera/call_center/concierge.py → No errors found
```

## 4. Live smoke (optional, deferred)

`scripts/smoke_concierge_live.py` was not re-run as part of this phase
— the live-Qdrant + live-Anthropic baseline already established in
Phase 3 stands. The visible change for live runs is that the printed
JSON now includes a `timings` key on every response.

To re-baseline:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/smoke_concierge_live.py
```

Expected new output line per call (example):

```
timings: {'decompose_ms': 412.3, 'retrieve_ms': 184.1, 'render_ms': 901.4, 'total_ms': 1497.8}
```
