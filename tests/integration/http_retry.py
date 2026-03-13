"""Small retry helper for integration tests against Ollama/LM Studio.

Provides post_with_retry(endpoints, payload, ...) which tries endpoints until
successful 2xx response or timeout. Treats common transient HTTP codes and
response bodies mentioning downloading/loading as transient.

Set environment variable INTEGRATION_DEBUG=1 to enable verbose attempt logging.
"""
import copy
import os
import time
from typing import List, Tuple
import requests

TRANSIENT_STATUS = {429, 502, 503, 504, 425}


def _make_payload_variants(payload: dict) -> List[dict]:
    """Return sensible payload variants to try against different server APIs.

    - original payload
    - payload without 'format' (some endpoints don't accept it)
    - OpenAI-compatible 'response_format' variant (for /v1 endpoints)
    """
    variants = []
    base = copy.deepcopy(payload)
    variants.append(base)

    no_format = copy.deepcopy(payload)
    if "format" in no_format:
        no_format.pop("format")
    variants.append(no_format)

    # create a conservative response_format mapping if 'format' requested JSON
    resp_fmt = copy.deepcopy(no_format)
    if payload.get("format") == "json":
        # Request a permissive JSON schema (object) to encourage JSON output
        resp_fmt["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }
        # remove any 'format' just in case
        resp_fmt.pop("format", None)
        variants.append(resp_fmt)

    # Deduplicate variants by stringifying
    seen = set()
    out = []
    for v in variants:
        s = json_safe_str(v)
        if s in seen:
            continue
        seen.add(s)
        out.append(v)
    return out


def json_safe_str(v: dict) -> str:
    try:
        import json

        return json.dumps(v, sort_keys=True)
    except Exception:
        return str(v)


def _debug(msg: str) -> None:
    if os.getenv("INTEGRATION_DEBUG"):
        try:
            print(msg, flush=True)
        except Exception:
            pass


def post_with_retry(
    endpoints: List[str],
    payload: dict,
    *,
    overall_timeout: float = 120.0,
    per_request_timeout: float = 30.0,
    sleep_between_rounds: float = 2.0,
) -> Tuple[requests.Response, str]:
    """
    Try POSTing `payload` to each endpoint in `endpoints` in order.
    Retry rounds until `overall_timeout` elapses. Treat common transient
    HTTP statuses and responses mentioning 'downloading' / 'loading'
    as transient (model still downloading/initializing).
    Returns (response, endpoint) on success, raises RuntimeError on timeout.
    """
    start = time.time()
    session = requests.Session()

    def looks_like_model_downloading(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        return any(
            k in t
            for k in (
                "downloading",
                "initializing",
                "loading",
                "still downloading",
                "model is downloading",
                "downloading model",
            )
        )

    last_exception = None
    # Precompute variants
    variants = _make_payload_variants(payload)

    _debug(f"post_with_retry: endpoints={endpoints} variants={len(variants)} overall_timeout={overall_timeout}")

    attempt = 0
    while time.time() - start < overall_timeout:
        attempt += 1
        for ep in endpoints:
            for idx, pvar in enumerate(variants):
                _debug(f"attempt #{attempt} -> POST {ep} (variant {idx})")
                try:
                    r = session.post(ep, json=pvar, timeout=per_request_timeout)
                except requests.RequestException as e:
                    last_exception = e
                    _debug(f"request exception for {ep} variant {idx}: {e}")
                    # connection/timeout errors -> treat as transient and try next endpoint
                    continue

                _debug(f"response {r.status_code} from {ep} (variant {idx})")

                # success codes
                if 200 <= r.status_code < 300:
                    _debug(f"success -> using endpoint {ep} (variant {idx})")
                    return r, ep

                # transient HTTP statuses: retry
                if r.status_code in TRANSIENT_STATUS:
                    last_exception = RuntimeError(f"Transient status {r.status_code} from {ep}")
                    _debug(f"transient status {r.status_code} from {ep}")
                    continue

                # if body indicates model is still downloading/initializing -> retry
                try:
                    body = r.text or ""
                except Exception:
                    body = ""
                if looks_like_model_downloading(body):
                    last_exception = RuntimeError(f"Model downloading/initializing detected from {ep}")
                    _debug(f"model downloading/initializing detected from {ep}: body snippet: {body[:200]}")
                    continue

                # For 401/403: propagate immediately (auth problem)
                if r.status_code in (401, 403):
                    _debug(f"auth error {r.status_code} from {ep}")
                    r.raise_for_status()

                # For other non-transient statuses, raise to surface errors
                _debug(f"non-transient error {r.status_code} from {ep} body snippet: {body[:500]}")
                r.raise_for_status()

        # wait before next round of attempts
        _debug(f"sleeping {sleep_between_rounds}s before next round")
        time.sleep(sleep_between_rounds)

    raise RuntimeError(f"All endpoints failed or model not ready within {overall_timeout} seconds")
