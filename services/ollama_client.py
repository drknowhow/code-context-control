"""Thin HTTP client for Ollama — stdlib only, no new dependencies.

All calls have a 10s timeout and return None on failure,
so callers can gracefully degrade to non-LLM paths.

Includes integrated LLM response cache (formerly llm_cache.py).
"""
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_BASE_URL = "http://localhost:11434"
_TIMEOUT = 30  # seconds


class LLMCache:
    """Persistent disk cache for LLM results."""

    def __init__(self, cache_dir: str = ".c3/cache/llm"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_key(self, prompt: str, model: str, system: str = "", **options) -> str:
        opt_str = json.dumps(options, sort_keys=True)
        key_content = f"{model}:{system}:{prompt}:{opt_str}"
        return hashlib.md5(key_content.encode()).hexdigest()

    def get(self, prompt: str, model: str, system: str = "", **options) -> Optional[str]:
        key = self._get_key(prompt, model, system, **options)
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("response")
            except Exception:
                pass
        return None

    def set(self, prompt: str, model: str, response: str, system: str = "", **options):
        key = self._get_key(prompt, model, system, **options)
        cache_file = self.cache_dir / f"{key}.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "model": model,
                    "system": system,
                    "prompt": prompt,
                    "options": options,
                    "response": response
                }, f, indent=2)
        except Exception:
            pass


class OllamaClient:
    """Minimal Ollama REST client using urllib."""

    _AVAIL_TTL_SECONDS = 10.0

    def __init__(self, base_url: str = DEFAULT_BASE_URL, cache_dir: str = ".c3/cache/llm"):
        self.base_url = base_url.rstrip("/")
        self.cache = LLMCache(cache_dir)
        self._avail_value: bool | None = None
        self._avail_ts: float = 0.0

    # ── Availability ──────────────────────────────────────

    def is_available(self, timeout: int | None = None) -> bool:
        """Check if Ollama is reachable. Cached for ``_AVAIL_TTL_SECONDS`` to
        avoid storm when many background agents poll concurrently."""
        import time
        now = time.monotonic()
        if self._avail_value is not None and (now - self._avail_ts) < self._AVAIL_TTL_SECONDS:
            return self._avail_value
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT):
                self._avail_value = True
        except Exception:
            self._avail_value = False
        self._avail_ts = now
        return self._avail_value

    # ── Models ────────────────────────────────────────────

    def list_models(self) -> list[str] | None:
        """Return list of locally available model names, or None on failure."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return None

    def has_model(self, model: str) -> bool:
        """Check if a specific model is pulled locally."""
        models = self.list_models()
        if models is None:
            return False
        return any(model in m or m.startswith(model) for m in models)

    # ── Embeddings ────────────────────────────────────────

    def embed(self, text: str, model: str = "nomic-embed-text") -> list[float] | None:
        """Generate embedding vector for text. Returns None on failure."""
        try:
            payload = json.dumps({"model": model, "input": text}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) > 0:
                return embeddings[0]
            return None
        except Exception:
            return None

    def embed_batch(self, texts: list[str], model: str = "nomic-embed-text") -> list[list[float]] | None:
        """Embed multiple texts in one call. Returns None on failure."""
        try:
            payload = json.dumps({"model": model, "input": texts}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT * 3) as resp:
                data = json.loads(resp.read())
            return data.get("embeddings")
        except Exception:
            return None

    # ── Generation ────────────────────────────────────────

    def generate(self, prompt: str, model: str = "gemma3n:latest",
                 system: str = "", temperature: float = 0.3,
                 max_tokens: int = 512, num_ctx: int = 4096,
                 stream: bool = False, timeout: int = 60):
        """Generate text completion. Returns string if stream=False, or generator if True."""
        options = {"temperature": temperature, "num_predict": max_tokens, "num_ctx": num_ctx}

        if not stream:
            cached = self.cache.get(prompt, model, system, **options)
            if cached:
                return cached

        try:
            body = {
                "model": model,
                "prompt": prompt,
                "stream": stream,
                "options": options,
            }
            if system:
                body["system"] = system
            payload = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            if stream:
                return self._stream_generator(req)

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())

            response = data.get("response")
            if response:
                self.cache.set(prompt, model, response, system, **options)
            return response
        except Exception:
            return None

    def _stream_generator(self, request):
        """Internal generator for streaming responses."""
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                for line in resp:
                    if not line:
                        continue
                    chunk = json.loads(line.decode("utf-8"))
                    if "response" in chunk:
                        yield chunk["response"]
                    if chunk.get("done"):
                        break
        except Exception as e:
            yield f"\n[Streaming Error: {e}]"
