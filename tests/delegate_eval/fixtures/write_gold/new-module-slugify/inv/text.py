"""Text helpers."""
import re

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def slugify(title: str, max_len: int = 60) -> str:
    """A URL slug: lowercase ASCII letters and digits joined by single hyphens."""
    slug = _SEPARATORS.sub("-", title.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "item"
