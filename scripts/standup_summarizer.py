#!/usr/bin/env python3
"""Confirm-gated GitHub evidence summarizer for the morning standup.

This is the only LLM call in the v0.3.1 standup path. It is intentionally small:
one OpenAI-compatible HTTP POST to a configured Ollama endpoint (local no-auth
proxy by default, or the authenticated Ollama Cloud surface when an API key is
set), no tools, no session, no model fallback, hard output validation, and
deterministic fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_config
from utils import _atomic_write

PROMPT_VERSION = "v0.3.1-u4-2026-06-24"
AREA_TAXONOMY = frozenset({"eng", "sales-ops", "marketing", "unclassified"})
MAX_BULLET_CHARS = 180
MAX_RESPONSE_BYTES = 2_000_000
TRANSLATION_UNAVAILABLE = "translation unavailable; deterministic GitHub evidence grouping shown"

_WHITESPACE_RE = re.compile(r"\s+")
_MENTION_RE = re.compile(r"(?<![\w.-])@([A-Za-z0-9_.-]+)")
_MARKDOWN_RE = re.compile(r"[*_`>#\[\]()]")

HttpPost = Callable[[str, dict[str, Any], int], tuple[int, str | bytes]]


def _single_line(value: Any) -> str:
    text = str(value or "")
    without_controls = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in text
    )
    return _WHITESPACE_RE.sub(" ", without_controls).strip()


def _clean_display_text(value: Any, *, max_chars: int = MAX_BULLET_CHARS) -> str:
    text = _single_line(value)
    text = _MENTION_RE.sub(r"\1", text)
    text = _MARKDOWN_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip(" -")
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _normalise_candidates(github_candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    minimal: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in github_candidates:
        evidence_id = _single_line(candidate.get("evidence_id") or candidate.get("evidence_hash"))
        title = _single_line(candidate.get("match_title"))
        if not evidence_id or not title or evidence_id in seen:
            continue
        seen.add(evidence_id)
        minimal.append({"evidence_id": evidence_id, "title": title})
    return sorted(minimal, key=lambda item: (item["evidence_id"], item["title"]))


def _canonical_input(minimal: list[dict[str, str]]) -> str:
    return json.dumps(minimal, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _cache_key(canonical_input: str, *, model: str) -> str:
    payload = json.dumps(
        {
            "input": canonical_input,
            "model": model,
            "prompt_version": PROMPT_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path() -> Path:
    return cos_config.state_dir() / "standup-summarizer-cache.json"


def _cache_lock_path() -> Path:
    return cos_config.state_dir() / "standup-summarizer-cache.lock"


@contextmanager
def _cache_flock() -> Iterator[None]:
    cos_config.state_dir()
    with _cache_lock_path().open("a", encoding="utf-8") as lock_handle:
        try:
            os.fchmod(lock_handle.fileno(), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _read_cache() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_cache(cache: dict[str, Any]) -> None:
    _atomic_write(_cache_path(), json.dumps(cache, indent=2, sort_keys=True) + "\n")


def _cached_result(cache_key: str) -> dict[str, Any] | None:
    with _cache_flock():
        cache = _read_cache()
        result = cache.get(cache_key)
    return result if _is_structural_cache_hit(result) else None


def _is_structural_cache_hit(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("draft") is not True or result.get("confirmed") is not False:
        return False
    if result.get("prompt_version") != PROMPT_VERSION:
        return False
    if not isinstance(result.get("translated"), bool):
        return False
    bullets = result.get("bullets")
    if not isinstance(bullets, list):
        return False
    for item in bullets:
        if not isinstance(item, dict):
            return False
        if item.get("area") not in AREA_TAXONOMY:
            return False
        bullet = item.get("bullet")
        if not isinstance(bullet, str) or not bullet.strip():
            return False
    return True


def _store_cache(cache_key: str, result: dict[str, Any]) -> None:
    with _cache_flock():
        cache = _read_cache()
        cache[cache_key] = result
        _write_cache(cache)


def _system_prompt() -> str:
    return (
        "You turn minimal GitHub commit/PR metadata into outcome-oriented standup draft bullets. "
        "Return JSON only: a list of objects with evidence_id, area, bullet. "
        "area must be one of eng, sales-ops, marketing, unclassified. "
        "Use only supplied evidence_ids. Do not mark anything done."
    )


def _request_payload(
    minimal: list[dict[str, str]],
    *,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "prompt_version": PROMPT_VERSION,
                        "github_evidence": minimal,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            },
        ],
    }


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    api_key: str = "",
) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("unsupported summarizer url scheme")
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status), _read_response_bounded(response)
    except urllib.error.HTTPError as exc:
        return int(exc.code), _read_response_bounded(exc)


def _read_response_bounded(response: Any) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("summarizer response too large")
    return body


def _extract_model_content(response_body: str | bytes) -> Any:
    text = response_body.decode("utf-8", errors="replace") if isinstance(response_body, bytes) else response_body
    envelope = json.loads(text)
    if isinstance(envelope, list):
        return envelope
    if not isinstance(envelope, dict):
        raise ValueError("summarizer response must be a JSON object or list")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("summarizer response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("summarizer response missing message content")
    # A fenced-JSON model would always fall back; the pinned model returns raw JSON.
    return json.loads(content)


def _validate_output(raw_output: Any, valid_ids: set[str]) -> list[dict[str, str]]:
    if not isinstance(raw_output, list):
        raise ValueError("summarizer output must be a list")
    bullets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_output:
        if not isinstance(item, dict):
            raise ValueError("summarizer output item must be an object")
        evidence_id = _single_line(item.get("evidence_id"))
        if evidence_id not in valid_ids:
            continue
        area = _single_line(item.get("area")).casefold()
        if area not in AREA_TAXONOMY:
            area = "unclassified"
        bullet = _clean_display_text(item.get("bullet"))
        if not bullet:
            continue
        dedupe_key = (evidence_id, bullet.casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        bullets.append({"evidence_id": evidence_id, "area": area, "bullet": bullet})
    if not bullets:
        raise ValueError("summarizer output referenced no valid evidence ids")
    return bullets


def _classify_area(title: str) -> str:
    lowered = title.casefold()
    if any(token in lowered for token in ("sales", "customer", "pipeline", "crm", "deal", "revenue")):
        return "sales-ops"
    if any(token in lowered for token in ("marketing", "campaign", "website", "landing", "content", "brand")):
        return "marketing"
    if any(token in lowered for token in ("fix", "bug", "api", "test", "deploy", "refactor", "sync", "adapter", "cache")):
        return "eng"
    return "unclassified"


def _fallback(minimal: list[dict[str, str]], *, model: str, disclosure: str | None = TRANSLATION_UNAVAILABLE) -> dict[str, Any]:
    bullets = [
        {
            "evidence_id": item["evidence_id"],
            "area": _classify_area(item["title"]),
            "bullet": _clean_display_text(item["title"]),
        }
        for item in minimal
        if _clean_display_text(item["title"])
    ]
    return {
        "bullets": bullets,
        "translated": False,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "disclosure": disclosure if bullets else None,
        "draft": True,
        "confirmed": False,
    }


def summarize(
    github_candidates: list[dict[str, Any]],
    *,
    http_post: HttpPost | None = None,
    enabled: bool | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Return a confirm-gated draft summary for GitHub evidence candidates."""
    exact_model = model or cos_config.standup_summarizer_model()
    minimal = _normalise_candidates(github_candidates)
    if not minimal:
        return _fallback(minimal, model=exact_model, disclosure=None)

    if enabled is None:
        enabled = cos_config.standup_summarizer_enabled()
    if not enabled:
        return _fallback(minimal, model=exact_model)

    canonical = _canonical_input(minimal)
    key = _cache_key(canonical, model=exact_model)
    cached = _cached_result(key)
    if cached is not None:
        return cached

    if http_post is not None:
        poster = http_post
    else:
        exact_key = api_key if api_key is not None else cos_config.standup_summarizer_api_key()

        def poster(url: str, body: dict[str, Any], timeout: int) -> tuple[int, bytes]:
            return _http_post_json(url, body, timeout, api_key=exact_key)

    try:
        payload = _request_payload(
            minimal,
            model=exact_model,
            max_tokens=max_tokens or cos_config.standup_summarizer_max_tokens(),
        )
        status, body = poster(
            base_url or cos_config.standup_summarizer_base_url(),
            payload,
            timeout_seconds or cos_config.standup_summarizer_timeout_seconds(),
        )
        if status != 200:
            raise ValueError("summarizer returned non-200")
        bullets = _validate_output(_extract_model_content(body), {item["evidence_id"] for item in minimal})
    except Exception:  # noqa: BLE001 -- every failure degrades to deterministic fallback
        return _fallback(minimal, model=exact_model)

    result = {
        "bullets": bullets,
        "translated": True,
        "model": exact_model,
        "prompt_version": PROMPT_VERSION,
        "disclosure": None,
        "draft": True,
        "confirmed": False,
    }
    try:
        _store_cache(key, result)
    except Exception:  # noqa: BLE001 -- best-effort cache; a write failure must not abort the standup
        pass
    return result
