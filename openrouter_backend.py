"""OpenRouter backend: HTTP client construction, 429 backoff, and the
killable multiprocessing worker AUTO uses to bound an OpenRouter call by a
hard wall-clock deadline.

agent.py decomposition (Faz 2, Adım 6 -- riskier-layer session). Extracted
verbatim from agent.py. _interruptible_sleep/_sleep move here rather than
staying generic dispatch machinery: their only production caller,
_or_create_with_backoff, is itself OpenRouter-only.

_find_claude_cli stays BEHIND in agent.py despite living in the same
textual region before this move -- it is _build_client's helper (Claude
CLI discovery), unrelated to OpenRouter.

The one subsystem here with a REAL, if accidental, Windows safety
dependency: _run_openrouter_killable's multiprocessing.Process() spawn has
no `creationflags` lever (unlike subprocess.run/Popen elsewhere in this
app) -- on a consoleless parent (pm2/headless), a console-subsystem child
can flash a window or hang forever at Process.start(). This app's actual
fix for that is sandbox.py's `multiprocessing.set_executable(pythonw.exe)`
(gated on PM2_HOME), a PROCESS-GLOBAL side effect this module benefits
from only by accident, via server.py -> web/routes/loop.py (eager) ->
loop_runner.py (eager `from sandbox import ...`) -> sandbox.py's
import-time set_executable() call running before this module's first
spawn. loop_runner.py looks legacy (its only other consumer is
legacy/streamlit_app.py); if that import chain is ever removed or made
lazy, OpenRouter's multiprocessing path silently loses this protection
with no test to catch the regression -- see
tests/test_regression_anchors.py::TestNoConsoleWindow's sibling AST-guard
test asserting loop_runner.py still imports sandbox.py at module level.
Making this dependency explicit (this module calling set_executable
itself, independent of sandbox.py/import order) is a deliberately deferred,
behavior-changing follow-up, not done here.

Wiki References
---------------
See: [[llm_maliyet_kaldiraclari]].
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from typing import Any

from llm_client import (
    _LLM_CONTROL,
    _check_llm_cancelled,
    _poll_until_deadline,
    current_effort,
)


def _interruptible_sleep(seconds: float) -> None:
    # Non-AUTO callers have no cancellation source; preserve one sleep call
    # (also keeps deterministic unit tests fast when _sleep is replaced).
    if not callable(getattr(_LLM_CONTROL, "cancel_check", None)):
        _sleep(max(0.0, float(seconds)))
        return
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        _check_llm_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _sleep(min(0.25, remaining))


# 429 geri çekilmesi. Toplam ~65 sn: ücretsiz uçların sınırı DAKİKA başına
# olduğu için bir dakikalık pencereyi kapsamayan bir plan hiçbir işe yaramaz
# (openai SDK'sının varsayılan ~0.5/1 sn'lik iki denemesi tam olarak bu yüzden
# yetersiz kalıyordu).
_OR_RETRY_WAITS = (5.0, 15.0, 45.0)

# Testler beklemeyi burdan yamalar. Global ``time.sleep``'i yamalamak süreçteki
# BAŞKA thread'leri de uykusuz bırakır (arka plan worker'ları) ve zamanlamaya
# duyarlı testleri uzaktan kırar — yama yüzeyi modüle ait olmalı.
_sleep = time.sleep


def _is_rate_limited(exc: Exception) -> bool:
    """429 mı? — `openai` paketi import edilmeden (o bağımlılık opsiyonel)."""
    return (
        getattr(exc, "status_code", None) == 429
        or getattr(exc, "code", None) == 429
        or type(exc).__name__ == "RateLimitError"
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    """Sunucunun söylediği bekleme süresi (``Retry-After``), varsa."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        return float(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None


def _or_create_with_backoff(or_model: str, **kwargs):
    """OpenRouter çağrısı; 429'da geri çekilip yeniden dener.

    429 kalıcı bir arıza değil, bir **zamanlama** arızasıdır: ücretsiz uçların
    dakika başına istek sınırı, ajan döngüsünün patlama şeklindeki çağrı
    temposuyla uyuşmaz. Yeniden denemeden çağıranın fallback'ine düşmek koşuyu
    rastgele kompozisyona çevirir ve bunu başarı gibi gösterir — yani sonuçları
    sessizce çöpe atar.

    Toplam bekleme ``NAUTILUS_OPENROUTER_429_MAX_WAIT`` ile sınırlı (varsayılan
    75 sn): bir dakikalık pencereyi kapatacak kadar uzun, koşunun STOP'a yanıtını
    kilitlemeyecek kadar kısa. Sunucu ``Retry-After`` söylüyorsa o kullanılır —
    tahminimiz sunucunun bilgisini ezmemeli.
    """
    budget = float(os.environ.get("NAUTILUS_OPENROUTER_429_MAX_WAIT", "75"))
    client = _get_openrouter_client()
    slept = 0.0
    for i, planned in enumerate(_OR_RETRY_WAITS):
        _check_llm_cancelled()
        try:
            return client.messages.create(model=or_model, **kwargs)
        except Exception as e:
            if not _is_rate_limited(e):
                raise
            wait = min(_retry_after_seconds(e) or planned, budget - slept)
            if wait <= 0:  # bütçe bitti — hata çağırana düşsün
                raise
            logging.warning(
                "OpenRouter 429 (%s) — %.0f sn bekleyip yeniden deneniyor (%d/%d)",
                or_model,
                wait,
                i + 1,
                len(_OR_RETRY_WAITS),
            )
            _interruptible_sleep(wait)
            slept += wait
    _check_llm_cancelled()
    return client.messages.create(model=or_model, **kwargs)


class _ORTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ORUsage:
    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        self.input_tokens = int(prompt_tokens or 0)
        self.output_tokens = int(completion_tokens or 0)
        self.cost_usd = float(cost_usd) if cost_usd is not None else None
        # Anthropic yüzeyiyle uyum için mevcut alanlar da sağlanır.
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _ORResponse:
    def __init__(
        self,
        text: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        stop_reason: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.content = [_ORTextBlock(text)]
        self.usage = _ORUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
        # Anthropic adlandırmasına çevrilir ("length" → "max_tokens"), böylece
        # _was_truncated tek bir alan okur; bkz. _create_message.
        self.stop_reason = stop_reason


class _OpenRouterProcessError(RuntimeError):
    """Serializable OpenRouter child-process failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _openrouter_usage_payload(usage: Any) -> dict[str, int | float | None]:
    raw = {}
    if usage is not None and hasattr(usage, "model_dump"):
        try:
            raw = usage.model_dump() or {}
        except Exception:
            raw = {}
    cost = raw.get("cost")
    if cost is None:
        cost = getattr(usage, "cost", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cost_usd": float(cost) if cost is not None else None,
    }


def _openrouter_process_main(conn, config: dict, request: dict) -> None:
    """Execute one provider call in an OS process the AUTO worker can kill."""

    try:
        from openai import OpenAI

        timeout = float(config.get("timeout") or 120.0)
        client = OpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            timeout=timeout,
            max_retries=0,
        )
        req = dict(request)
        req.pop("timeout", None)
        resp = client.chat.completions.create(**req)
        usage = _openrouter_usage_payload(getattr(resp, "usage", None))
        conn.send(
            {
                "ok": True,
                "text": (
                    (resp.choices[0].message.content or "") if resp.choices else ""
                ),
                "finish_reason": (
                    getattr(resp.choices[0], "finish_reason", None)
                    if resp.choices
                    else None
                ),
                **usage,
            }
        )
    except BaseException as exc:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        try:
            conn.send(
                {
                    "ok": False,
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "status_code": status,
                }
            )
        except Exception:
            pass
    finally:
        conn.close()


def _stop_provider_process(proc) -> None:
    if proc.is_alive():
        try:
            proc.kill()
        except AttributeError:
            proc.terminate()
    proc.join(timeout=2.0)


def _run_openrouter_killable(request: dict, config: dict, timeout: float) -> dict:
    """Run a synchronous SDK request behind a hard deadline and kill switch."""

    _check_llm_cancelled()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_openrouter_process_main,
        args=(child, {**config, "timeout": timeout}, request),
        daemon=True,
    )
    proc.start()
    child.close()

    def _or_step(wait_s):
        if parent.poll(wait_s):
            payload = parent.recv()
            if not payload.get("ok"):
                raise _OpenRouterProcessError(
                    f"{payload.get('type', 'OpenRouterError')}: "
                    f"{payload.get('message', 'provider call failed')}",
                    status_code=payload.get("status_code"),
                )
            return payload
        if not proc.is_alive():
            raise _OpenRouterProcessError(
                f"OpenRouter worker exited without a response (exit={proc.exitcode})"
            )
        return None

    try:
        return _poll_until_deadline(
            max(0.1, float(timeout)),
            0.1,
            _or_step,
            f"OpenRouter call exceeded {float(timeout):g}s hard deadline",
        )
    finally:
        _stop_provider_process(proc)
        parent.close()


class _OpenRouterMessages:
    def __init__(
        self,
        client: Any,
        extra_headers: dict[str, str] | None = None,
        process_config: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._extra_headers = extra_headers or {}
        self._process_config = process_config

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 0,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> _ORResponse:
        or_messages: list[dict[str, str]] = []
        if system:
            or_messages.append({"role": "system", "content": system})
        for m in messages:
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            if not isinstance(content, str):
                continue
            if role not in ("user", "assistant", "system"):
                role = "user"
            or_messages.append({"role": role, "content": content})

        req: dict[str, Any] = {
            "model": model,
            "messages": or_messages,
        }
        if max_tokens and max_tokens > 0:
            req["max_tokens"] = int(max_tokens)
        if timeout is not None:
            req["timeout"] = float(timeout)
        # OpenRouter'ın karşılığı `reasoning.effort` ve sözlüğü daha dar
        # (low/medium/high); Claude CLI'a özgü xhigh/max en yakın üst seviyeye
        # düşer. OpenAI SDK'sı bilinmeyen üst-düzey anahtarı reddeder, bu yüzden
        # extra_body ile gider. Akıl yürütmeyen uçlarda OpenRouter alanı yok
        # sayar — o durumda çağrı bugünküyle aynı kalır, hata vermez.
        effort = current_effort()
        if effort:
            or_effort = "high" if effort in ("xhigh", "max") else effort
            req["extra_body"] = {"reasoning": {"effort": or_effort}}
        if self._extra_headers:
            req["extra_headers"] = self._extra_headers

        if self._process_config is not None and callable(
            getattr(_LLM_CONTROL, "cancel_check", None)
        ):
            payload = _run_openrouter_killable(
                req, self._process_config, float(timeout or 120.0)
            )
            text = str(payload.get("text") or "")
            usage_payload = payload
            finish = payload.get("finish_reason")
        else:
            resp = self._client.chat.completions.create(**req)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            usage_payload = _openrouter_usage_payload(getattr(resp, "usage", None))
            finish = (
                getattr(resp.choices[0], "finish_reason", None)
                if resp.choices
                else None
            )
        return _ORResponse(
            text=text,
            prompt_tokens=usage_payload.get("prompt_tokens", 0),
            completion_tokens=usage_payload.get("completion_tokens", 0),
            stop_reason="max_tokens" if finish == "length" else finish,
            cost_usd=usage_payload.get("cost_usd"),
        )


class _OpenRouterClient:
    def __init__(
        self,
        client: Any,
        extra_headers: dict[str, str] | None = None,
        process_config: dict[str, str] | None = None,
    ) -> None:
        self.messages = _OpenRouterMessages(
            client,
            extra_headers=extra_headers,
            process_config=process_config,
        )


def _build_openrouter_client() -> _OpenRouterClient:
    """OpenRouter istemcisi — OPENROUTER_API_KEY gerekli.

    Hem tüm-uygulama backend'i (NAUTILUS_LLM_BACKEND=openrouter) hem de
    model seçicinin koşu-başına "or:<id>" yolu bu kurucuyu paylaşır.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OpenRouter model seçildi ama OPENROUTER_API_KEY ayarlı değil."
        )

    # openai paketi sadece bu backend kullanılınca gerekir.
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError(
            "OpenRouter backend requires the `openai` package. Install with: pip install openai"
        ) from e

    base_url = os.environ.get(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).strip()
    extra_headers: dict[str, str] = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    if referer:
        extra_headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_X_TITLE", "").strip()
    if title:
        extra_headers["X-Title"] = title

    timeout = float(os.environ.get("NAUTILUS_LLM_CALL_TIMEOUT", "120"))
    # Default matches the OpenAI SDK's own default (2): the synchronous
    # fallback branch in _OpenRouterMessages.create (used by any non-AUTO
    # caller — chat_edit_block, propose_custom_block outside an AUTO run) has
    # no other retry/backoff wrapping it, so zeroing this out by default would
    # turn a transient connection blip or 502/503 into a user-visible failure
    # that the SDK used to absorb silently.
    max_retries = int(os.environ.get("NAUTILUS_OPENROUTER_SDK_RETRIES", "2"))
    logging.info("LLM backend: openrouter (%s)", base_url)
    return _OpenRouterClient(
        OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max(0, max_retries),
        ),
        extra_headers=extra_headers,
        process_config={"base_url": base_url, "api_key": api_key},
    )


_or_client: _OpenRouterClient | None = None
_or_client_lock = threading.Lock()


def _get_openrouter_client() -> _OpenRouterClient:
    """Thread-safe OpenRouter singleton — "or:" model pinlerinin arka ucu."""
    global _or_client
    if _or_client is None:
        with _or_client_lock:
            if _or_client is None:
                _or_client = _build_openrouter_client()
    return _or_client
