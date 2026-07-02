"""OllamaBridge — Ollama cloud API client with Bearer auth for Oracle.

Uses the Ollama cloud service (https://ollama.com) by default.
Falls back to local Ollama (http://localhost:11434) if no API key is set.
API key can come from config or OLLAMA_API_KEY env var.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

_ORACLE_CACHE_DIR = Path.home() / ".c3" / "oracle" / "cache" / "llm"
_TIMEOUT = 30
_CACHE_MAX_ENTRIES = 512
# How long a model-capability probe result is trusted before re-probing.
_CAPS_TTL_SEC = 3600


class _Cache:
    """Simple disk cache for LLM responses (TTL + size-bounded)."""

    def __init__(self, cache_dir: Path = _ORACLE_CACHE_DIR, ttl_sec: int = 86400,
                 max_entries: int = _CACHE_MAX_ENTRIES):
        self._dir = cache_dir
        self._ttl = int(ttl_sec)
        self._max = int(max_entries)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, prompt: str, model: str, system: str = "", **opts) -> str:
        raw = f"{model}:{system}:{prompt}:{json.dumps(opts, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, prompt: str, model: str, system: str = "", **opts) -> Optional[str]:
        path = self._dir / f"{self._key(prompt, model, system, **opts)}.json"
        if path.exists():
            try:
                if self._ttl and (time.time() - path.stat().st_mtime) > self._ttl:
                    path.unlink(missing_ok=True)
                    return None
                return json.loads(path.read_text(encoding="utf-8")).get("response")
            except Exception:
                pass
        return None

    def set(self, prompt: str, model: str, response: str, system: str = "", **opts):
        path = self._dir / f"{self._key(prompt, model, system, **opts)}.json"
        try:
            path.write_text(json.dumps({
                "model": model, "prompt": prompt[:200],
                "response": response,
            }, indent=2), encoding="utf-8")
            self._prune()
        except Exception:
            pass

    def _prune(self):
        """Evict oldest entries (by mtime) once the cache exceeds its bound."""
        entries = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for stale in entries[: max(0, len(entries) - self._max)]:
            try:
                stale.unlink()
            except OSError:
                pass


class OllamaBridge:
    """Ollama cloud (or local) API client with Bearer token auth.

    Priority for API key:
      1. Explicit api_key parameter
      2. OLLAMA_API_KEY environment variable
    If neither is set, requests are sent without auth (works for local Ollama).
    """

    def __init__(
        self,
        base_url: str = "https://ollama.com",
        model: str = "gemma4:31b-cloud",
        api_key: str = "",
        cache_ttl_sec: int = 86400,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY", "")
        self._cache = _Cache(ttl_sec=cache_ttl_sec)
        # model → (probe_ts, supports_tools: bool | None). None = unknown.
        self._caps: dict[str, tuple[float, bool | None]] = {}

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, path: str, data: dict | None = None, timeout: int = _TIMEOUT):
        """Make an HTTP request to the Ollama API."""
        url = f"{self.base_url}{path}"
        if data is not None:
            payload = json.dumps(data).encode()
            req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
        else:
            req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    # ── Availability ──────────────────────────────────────

    def is_available(self, timeout: int | None = None) -> bool:
        """Check if Ollama API is reachable."""
        t = timeout or 5
        try:
            self._request("/api/tags", timeout=t)
            return True
        except Exception:
            pass
        # Cloud endpoints may not support /api/tags — try a HEAD-style chat
        try:
            url = f"{self.base_url}/api/chat"
            req = urllib.request.Request(url, headers=self._headers(), method="HEAD")
            urllib.request.urlopen(req, timeout=t)
            return True
        except urllib.error.HTTPError as e:
            # 4xx = the server answered (reachable); 5xx = it is failing —
            # reporting "available" on a 500 hid real outages from /api/health.
            return e.code < 500
        except Exception:
            return False

    # ── Models ────────────────────────────────────────────

    def show(self, model: str | None = None) -> dict | None:
        """Return /api/show metadata for a model, or None if unavailable."""
        try:
            return self._request("/api/show", data={"model": model or self.model},
                                 timeout=10)
        except Exception:
            return None

    def supports_tools(self, model: str | None = None) -> bool | None:
        """Whether a model supports native tool calling.

        Probes ``/api/show`` for a ``capabilities`` list containing ``tools``.
        Returns ``None`` when the endpoint is unavailable or doesn't report
        capabilities (some cloud deployments) — \"unknown\" is a distinct
        state: callers may attempt native tools and fall back on rejection.
        Results are cached per model (see ``set_tools_support`` for the
        negative-cache poke used by that fallback).
        """
        use_model = model or self.model
        cached = self._caps.get(use_model)
        if cached is not None and (time.time() - cached[0]) < _CAPS_TTL_SEC:
            return cached[1]
        caps: bool | None = None
        data = self.show(use_model)
        if isinstance(data, dict):
            listed = data.get("capabilities")
            if isinstance(listed, list):
                caps = "tools" in listed
        self._caps[use_model] = (time.time(), caps)
        return caps

    def set_tools_support(self, model: str | None, supported: bool | None) -> None:
        """Override the cached tools-capability for a model.

        Used to negative-cache after a live \"does not support tools\" rejection
        so later turns skip the doomed native attempt.
        """
        self._caps[model or self.model] = (time.time(), supported)

    def list_models(self) -> list[str] | None:
        """Return list of available model names."""
        try:
            data = self._request("/api/tags")
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return None

    def has_model(self, model: str | None = None) -> bool:
        """Check if a model is available.

        Cloud models (name contains 'cloud') won't appear in /api/tags,
        so we skip the tags check and rely on verify_model() instead.
        """
        target = model or self.model
        if "cloud" in target.lower():
            # Cloud models are not listed in /api/tags — can't check there
            return True  # defer to verify_model for actual reachability
        models = self.list_models()
        if models is None:
            return False
        return any(target in m or m.startswith(target) for m in models)

    def verify_model(self, model: str | None = None) -> bool:
        """Verify a model works by attempting a minimal generation.

        Cloud models may not appear in /api/tags and may only support
        /api/chat (not /api/generate). Try chat first (works for both
        local and cloud), fall back to generate for legacy endpoints.
        """
        log = logging.getLogger("oracle.bridge")
        use_model = model or self.model
        is_cloud = "cloud" in use_model.lower()
        # Cloud models need longer timeout for cold start
        timeout = 60 if is_cloud else 20

        # Try /api/chat first — works for both local and cloud models
        try:
            result = self.chat(
                [{"role": "user", "content": "Reply with only: OK"}],
                model=use_model, max_tokens=4, timeout=timeout,
            )
            if result is not None and len(result.strip()) > 0:
                log.info("Model %s verified via /api/chat", use_model)
                return True
            log.warning("Model %s: /api/chat returned empty response", use_model)
        except Exception as e:
            log.warning("Model %s: /api/chat failed: %s", use_model, e)

        # Fallback: /api/generate (some local-only or specific cloud models need this)
        try:
            result = self.generate("Reply with only: OK", model=use_model, max_tokens=4, timeout=timeout)
            if result is not None and len(result.strip()) > 0:
                log.info("Model %s verified via /api/generate", use_model)
                return True
            log.warning("Model %s: /api/generate returned empty response", use_model)
        except Exception as e:
            log.warning("Model %s: /api/generate failed: %s", use_model, e)

        log.error("Model %s: verification FAILED — model may not be available or needs longer timeout", use_model)
        return False

    # ── Generation ────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        num_ctx: int = 8192,
        model: str | None = None,
        timeout: int = 120,
        think: bool = True,
    ) -> str | None:
        """Generate text completion via Ollama API."""
        use_model = model or self.model
        options = {"temperature": temperature, "num_predict": max_tokens, "num_ctx": num_ctx}

        # Check cache
        cached = self._cache.get(prompt, use_model, system, **options)
        if cached:
            return cached

        try:
            body: dict = {
                "model": use_model,
                "prompt": prompt,
                "stream": False,
                "options": options,
            }
            if system:
                body["system"] = system
            if "cloud" in use_model.lower():
                body["think"] = think

            data = self._request("/api/generate", data=body, timeout=timeout)
            response = data.get("response") or data.get("content")
            if response:
                self._cache.set(prompt, use_model, response, system, **options)
            return response
        except Exception as e:
            logging.getLogger("oracle.bridge").warning("generate(%s) failed: %s", use_model, e)
            return None

    # ── Chat (alternative API) ────────────────────────────

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        num_ctx: int = 16384,
        timeout: int = 120,
        think: bool = True,
    ) -> str | None:
        """Chat completion via Ollama /api/chat endpoint."""
        use_model = model or self.model
        try:
            body = {
                "model": use_model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                    "num_ctx": num_ctx
                },
            }
            if "cloud" in use_model.lower():
                body["think"] = think

            data = self._request("/api/chat", data=body, timeout=timeout)
            msg = data.get("message", {})
            # Return content if present, otherwise thinking (for R1-style models)
            content = msg.get("content")
            if not content:
                content = msg.get("thinking")
            return content
        except Exception as e:
            logging.getLogger("oracle.bridge").warning("chat(%s) failed: %s", use_model, e)
            return None

    # ── Streaming Chat ────────────────────────────────────

    def stream_chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        num_ctx: int = 16384,
        timeout: int = 120,
        think: bool | None = True,
        tools: list[dict] | None = None,
    ):
        """Streaming chat completion — yields typed tuples as chunks arrive.

        Yields ``("thinking", str)``, ``("text", str)``, ``("tool_call",
        {"name", "arguments"})`` (native tool calling, when ``tools`` is
        passed), and a final ``("stats", dict)``.
        """
        use_model = model or self.model
        body = {
            "model": use_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": num_ctx,
            },
        }
        if think is not None:
            body["think"] = think
        if tools:
            body["tools"] = tools
        url = f"{self.base_url}/api/chat"
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=payload, headers=self._headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                if not line:
                    continue
                chunk = json.loads(line.decode("utf-8"))
                msg = chunk.get("message", {})
                # Models with think=True put reasoning in "thinking" field.
                thinking = msg.get("thinking", "")
                if thinking:
                    yield ("thinking", thinking)
                content = msg.get("content", "")
                if content:
                    yield ("text", content)
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    args = fn.get("arguments", {})
                    if isinstance(args, str):  # some models emit JSON strings
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {"_raw": args}
                    yield ("tool_call", {"name": fn.get("name", ""), "arguments": args or {}})
                if chunk.get("done"):
                    # Final chunk carries token stats from Ollama
                    stats = {}
                    for key in (
                        "total_duration", "load_duration",
                        "prompt_eval_count", "prompt_eval_duration",
                        "eval_count", "eval_duration",
                    ):
                        if key in chunk:
                            stats[key] = chunk[key]
                    if stats:
                        yield ("stats", stats)
                    break

    # ── Embeddings ────────────────────────────────────────

    def embed(self, text: str, model: str = "nomic-embed-text") -> list[float] | None:
        """Generate embedding vector."""
        try:
            data = self._request("/api/embed", data={"model": model, "input": text})
            embeddings = data.get("embeddings")
            return embeddings[0] if embeddings else None
        except Exception:
            return None

    def embed_batch(self, texts: list[str], model: str = "nomic-embed-text") -> list[list[float]] | None:
        """Embed multiple texts in one call."""
        try:
            data = self._request("/api/embed", data={"model": model, "input": texts}, timeout=_TIMEOUT * 3)
            return data.get("embeddings")
        except Exception:
            return None
