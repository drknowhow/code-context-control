"""Token counting and measurement utilities."""
import hashlib
import re
from functools import lru_cache

# Lazy-loaded tiktoken encoder
_encoder = None

# LRU cache keyed on content hash — avoids redundant tiktoken encodes
# within a session where the same code chunks are counted repeatedly.
_TOKEN_CACHE_MAX = 2048

@lru_cache(maxsize=_TOKEN_CACHE_MAX)
def _cached_count(content_hash: str, length: int, text: str) -> int:
    """Count tokens with LRU memoization. length param ensures hash collisions are safe."""
    encoder = _get_encoder()
    if encoder:
        return len(encoder.encode(text))
    tokens = re.findall(r'\w+|[^\w\s]', text)
    count = 0
    for t in tokens:
        if len(t) <= 4:
            count += 1
        else:
            count += max(1, len(t) // 4)
    return count

def _get_encoder():
    """Lazy-load tiktoken encoder (one-time cost)."""
    global _encoder
    if _encoder is None:
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("cl100k_base")
        except (ImportError, Exception):
            _encoder = False  # Sentinel: tiktoken unavailable
    return _encoder

def count_tokens(text: str) -> int:
    """
    Count tokens using tiktoken (cl100k_base) with LRU caching.
    Repeated calls with identical text hit the cache (~0ms vs ~1-2ms).
    Falls back to heuristic if tiktoken is unavailable.
    """
    if not text:
        return 0
    # For short strings (<50 chars), hashing overhead > tiktoken cost — skip cache
    if len(text) < 50:
        encoder = _get_encoder()
        if encoder:
            return len(encoder.encode(text))
        return len(re.findall(r'\w+|[^\w\s]', text))
    h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
    return _cached_count(h, len(text), text)

def measure_savings(original: str, compressed: str) -> dict:
    """Measure token savings between original and compressed text."""
    orig_tokens = count_tokens(original)
    comp_tokens = count_tokens(compressed)
    saved = orig_tokens - comp_tokens
    pct = (saved / orig_tokens * 100) if orig_tokens > 0 else 0
    return {
        "original_tokens": orig_tokens,
        "compressed_tokens": comp_tokens,
        "saved_tokens": saved,
        "savings_pct": round(pct, 1)
    }

def format_token_count(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)
