"""Small, stateless System One client for independently tested semantic skills."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class CallResult:
    response: dict[str, Any]
    input_tokens: int
    output_tokens: int
    elapsed_ms: int

    @property
    def answers(self) -> dict[str, Any]:
        return self.response["answers"]


class JevClient:
    def __init__(self, url: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float = 60,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 max_retries: int | None = None) -> None:
        self.url = url or os.environ.get("SYSTEM_ONE_URL") or "https://api.typesafe.ai/v1/systemone"
        self.model = (os.environ.get("SYSTEM_ONE_MODEL") or "jev-latest") if model is None else model
        self._api_key = (os.environ.get("SYSTEM_ONE_API_KEY") or "") if api_key is None else api_key
        self.timeout = timeout
        self.on_event = on_event
        self.max_retries = (max(0, int(os.environ.get("SYSTEM_ONE_RETRIES", "1")))
                            if max_retries is None else max(0, int(max_retries)))
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.failed_calls = 0
        self.failed_calls_without_usage = 0

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            self.on_event({"kind": kind, "call": self.calls,
                           "stats": self.usage_stats(), **payload})

    def usage_stats(self) -> dict[str, Any]:
        return {
            "jev_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "failed_calls": self.failed_calls,
            "usage_complete": self.failed_calls_without_usage == 0,
        }

    def _record_usage(self, data: Any, *, failed: bool = False) -> None:
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        has_usage = "input_tokens" in usage or "output_tokens" in usage
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        if failed:
            self.failed_calls += 1
            if not has_usage:
                self.failed_calls_without_usage += 1

    def call(self, state: Any, questions: dict[str, Any]) -> CallResult:
        if not questions:
            raise ValueError("At least one question is required")
        payload = {"state": state, "questions": questions}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "IntentSQL/0.1.0-alpha.1"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        retryable = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            request = Request(
                self.url,
                data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            started = time.monotonic()
            # Emit the exact JSON body sent to System One. Authentication lives
            # in HTTP headers and is intentionally never included in UI/event telemetry.
            self._emit("call_start", state=state, questions=questions,
                       request_payload=payload, attempt=attempt)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raw_detail = exc.read().decode("utf-8", errors="replace")
                code = exc.code
                retry_after = None
                try:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                except Exception:
                    retry_after = None
                exc.close()
                try:
                    error_data = json.loads(raw_detail)
                except Exception:
                    error_data = None
                self._record_usage(error_data, failed=True)
                message = {401: "API key was not accepted. Check Connection settings.",
                           403: "The provider denied access. Check your key and model.",
                           429: "Provider rate limit reached. Wait a moment and try again."}.get(
                               code, f"System One returned HTTP {code}. Try again shortly.")
                self._emit("call_error", message=message,
                           latency_ms=round((time.monotonic() - started) * 1000))
                if code in retryable and attempt <= self.max_retries:
                    try:
                        delay = min(5.0, max(0.0, float(retry_after))) if retry_after else 0.5 * attempt
                    except (TypeError, ValueError):
                        delay = 0.5 * attempt
                    self._emit("call_retry", message=message, attempt=attempt + 1,
                               retry_in_ms=round(delay * 1000))
                    time.sleep(delay)
                    continue
                raise RuntimeError(message) from exc
            except (URLError, TimeoutError, OSError) as exc:
                # A 60-second transport timeout is already expensive. Do not
                # automatically double it; only immediate HTTP overload errors
                # are retried above.
                self._record_usage(None, failed=True)
                message = "Could not reach System One. Check the endpoint and your connection, then retry."
                self._emit("call_error", message=message)
                raise RuntimeError(message) from exc
            except (ValueError, UnicodeError) as exc:
                self._record_usage(None, failed=True)
                self._emit("call_error", message="Provider returned invalid JSON.")
                raise RuntimeError("Provider returned invalid JSON. Check the endpoint or retry.") from exc
            self._record_usage(data, failed=False)
            if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                self.failed_calls += 1
                if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
                    self.failed_calls_without_usage += 1
                self._emit("call_error", message="System One returned no answers object")
                raise RuntimeError("System One returned no answers object")
            usage = data.get("usage") or {}
            elapsed = round((time.monotonic() - started) * 1000)
            self._emit("call_end", answers=data["answers"], usage=usage, response_payload=data,
                       latency_ms=elapsed)
            return CallResult(
                response=data,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                elapsed_ms=elapsed,
            )
