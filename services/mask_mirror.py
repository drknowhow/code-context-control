"""Mask Guard mirror — content-addressed materialized views.

Implements docs/mask-guard.md §2. A masked path is never rendered in-flight:
it is materialized once per (source bytes x preset x params x transformer
version) into an immutable artifact, validated, recorded in a manifest, and
served from there. Every content surface reads the SAME artifact, which is
what makes cross-surface differencing impossible (§2).

Layout::

    ~/.c3/masked/<project-id>/
        manifest.json          # rel-path -> view record
        salt                   # project-local pseudonym salt, never exposed
        views/<view-hash>      # immutable rendered bytes

Staleness is checked on every serve. A stale view is regenerated, and if it
cannot be regenerated the read REFUSES — a stale twin is never served.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from services import access_guard
from services.mask_presets import TRANSFORMER_VERSION, MaskRenderError, render


class MaskUnavailable(Exception):
    """The masked view cannot be served. The read must refuse with .message."""

    def __init__(self, message: str, reason: str = "render_failed"):
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass(frozen=True)
class MaskedView:
    text: str
    view_hash: str
    preset: str
    stats: dict
    rule: access_guard.MaskRule

    def with_header(self, path: str = "") -> str:
        """Rendered content prefixed with the §5 disclosure banner."""
        return access_guard.mask_header(self.rule, path) + "\n\n" + self.text


def _project_id(project_path) -> str:
    """Stable per-project directory name; readable prefix + path digest."""
    resolved = str(Path(project_path).resolve()).replace("\\", "/")
    digest = hashlib.blake2b(resolved.casefold().encode("utf-8"),
                             digest_size=6).hexdigest()
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-"
                   for ch in Path(resolved).name)[:32] or "project"
    return f"{stem}-{digest}"


def mirror_root(project_path) -> Path:
    base = Path.home() / ".c3" / "masked" / _project_id(project_path)
    return base


def _salt(project_path) -> str:
    """Project-local pseudonym salt. Created once, never leaves this machine.

    Without it, ``redact_columns`` pseudonyms for a low-cardinality column
    could be brute-forced back to their real values from the digest alone.
    """
    root = mirror_root(project_path)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "salt"
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except Exception:
            pass
    value = hashlib.blake2b(os.urandom(32), digest_size=16).hexdigest()
    tmp = path.with_name("salt.tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)
    return value


def view_hash(source_bytes: bytes, rule: access_guard.MaskRule) -> str:
    """The content address. Everything that can change the output is in it.

    Deliberately includes ``TRANSFORMER_VERSION`` and the preset params: a
    rule edit or an engine bump must mint a NEW address rather than silently
    reuse a view rendered under the old policy (docs/mask-guard.md §6, row 1).
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(hashlib.blake2b(source_bytes, digest_size=16).digest())
    h.update(b"\x00" + rule.preset.encode("utf-8"))
    h.update(b"\x00" + json.dumps(rule.params_dict, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8"))
    h.update(b"\x00" + str(TRANSFORMER_VERSION).encode("ascii"))
    return h.hexdigest()


# ── Manifest ────────────────────────────────────────────────────────────────

def _manifest_path(project_path) -> Path:
    return mirror_root(project_path) / "manifest.json"


def load_manifest(project_path) -> dict:
    path = _manifest_path(project_path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt manifest must not serve stale views: treat as empty so
        # every path re-renders and re-validates.
        return {}


def _save_manifest(project_path, manifest: dict) -> None:
    path = _manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("manifest.json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ── Build + serve ───────────────────────────────────────────────────────────

def _views_dir(project_path) -> Path:
    d = mirror_root(project_path) / "views"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_view(path: Path) -> str:
    """Byte-faithful artifact read.

    ``newline=""`` is load-bearing: universal-newline translation would turn a
    CRLF view back into LF on read, so the SAME view would differ between a
    fresh render and a cache hit. That is precisely the cross-surface
    inconsistency an agent can difference (docs/mask-guard.md §2).
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_view_atomic(dest: Path, text: str) -> None:
    """Write-once immutable artifact.

    Windows-safe: render to a unique temp name in the SAME directory, then
    ``os.replace``. Two processes racing on the same view hash produce
    byte-identical content, so whichever lands last is still correct.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-view-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def _source_signature(source: Path) -> dict:
    st = source.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def build_view(rel_path: str, rule: access_guard.MaskRule, project_path,
               *, compressor=None) -> MaskedView:
    """Render, validate and materialize one view. Raises MaskUnavailable."""
    project = Path(project_path).resolve()
    source = (project / rel_path).resolve()
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        raise MaskUnavailable(
            f"masked source not found: {rel_path}", "missing") from exc
    except Exception as exc:
        raise MaskUnavailable(
            f"cannot read masked source {rel_path}: {exc}", "unreadable"
        ) from exc

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaskUnavailable(
            f"{rel_path} is not UTF-8 text; Mask Guard cannot render it, and "
            "serving the original would defeat the rule", "binary") from exc

    vh = view_hash(raw, rule)
    dest = _views_dir(project) / vh

    if compressor is None and rule.preset == "signatures_only":
        from services.compressor import CodeCompressor
        compressor = CodeCompressor(cache_dir=str(project / ".c3" / "cache"),
                                    project_root=str(project))

    if dest.is_file():
        try:
            cached = _read_view(dest)
            return MaskedView(cached, vh, rule.preset, {"cached": True}, rule)
        except Exception:
            pass  # unreadable artifact: fall through and re-render

    try:
        result = render(text, rule.preset, rule.params_dict,
                        salt=_salt(project), path=rel_path,
                        compressor=compressor)
    except MaskRenderError as exc:
        raise MaskUnavailable(str(exc), "render_failed") from exc

    _write_view_atomic(dest, result.text)

    manifest = load_manifest(project)
    manifest[rel_path.replace("\\", "/")] = {
        "view_hash": vh,
        "preset": rule.preset,
        "params": rule.params_dict,
        "glob": rule.glob,
        "scope": rule.scope,
        "transformer_version": TRANSFORMER_VERSION,
        "source": _source_signature(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "validated": True,
        "built_at": int(time.time()),
        "stats": result.stats,
    }
    _save_manifest(project, manifest)
    return MaskedView(result.text, vh, rule.preset, result.stats, rule)


def get_view(rel_path: str, rule: access_guard.MaskRule, project_path,
             *, compressor=None) -> MaskedView:
    """Serve the current view, rebuilding when stale. Never serves a stale twin.

    The staleness check is cheap (size + mtime) and then authoritative (the
    view hash is recomputed from the actual bytes on any signature mismatch),
    because mtime alone is not trustworthy across editors and VCS checkouts.
    """
    project = Path(project_path).resolve()
    rel = rel_path.replace("\\", "/")
    record = load_manifest(project).get(rel)
    if record:
        source = (project / rel)
        try:
            live = _source_signature(source)
        except Exception:
            live = None
        fresh = (
            live is not None
            and record.get("source") == live
            and record.get("transformer_version") == TRANSFORMER_VERSION
            and record.get("preset") == rule.preset
            and record.get("params") == rule.params_dict
        )
        if fresh:
            view_file = _views_dir(project) / str(record.get("view_hash"))
            if view_file.is_file():
                try:
                    return MaskedView(_read_view(view_file),
                                      str(record.get("view_hash")),
                                      rule.preset,
                                      record.get("stats") or {}, rule)
                except Exception:
                    pass  # fall through to rebuild
    return build_view(rel, rule, project, compressor=compressor)


def render_for_path(path, project_path, *, compressor=None):
    """(MaskedView | None) for *path*. ``None`` when the path is not masked.

    The single entry point for content surfaces. Raises ``AccessDenied`` for
    denied paths and ``MaskUnavailable`` when a masked path cannot be served.
    """
    v = access_guard.verdict(path, "read", str(project_path))
    if v.denial:
        raise access_guard.AccessDenied(
            v.denial, access_guard.refusal(v.denial, path, "read"))
    if not v.masked:
        return None
    project = Path(project_path).resolve()
    try:
        rel = str(Path(path).resolve().relative_to(project)).replace("\\", "/")
    except Exception:
        rel = str(path).replace("\\", "/")
    return get_view(rel, v.mask_rule, project, compressor=compressor)


# ── Maintenance ─────────────────────────────────────────────────────────────

def purge_path(rel_path: str, project_path) -> bool:
    """Drop one path's manifest record (its view artifact is GC'd later)."""
    project = Path(project_path).resolve()
    manifest = load_manifest(project)
    if manifest.pop(rel_path.replace("\\", "/"), None) is None:
        return False
    _save_manifest(project, manifest)
    return True


def gc(project_path) -> dict:
    """Delete view artifacts no manifest record references any more."""
    project = Path(project_path).resolve()
    live = {str(r.get("view_hash")) for r in load_manifest(project).values()}
    removed = 0
    views = mirror_root(project) / "views"
    if views.is_dir():
        for artifact in views.iterdir():
            if artifact.is_file() and artifact.name not in live \
                    and not artifact.name.startswith(".tmp-"):
                try:
                    artifact.unlink()
                    removed += 1
                except Exception:
                    pass
    return {"removed": removed, "live": len(live)}


def clear(project_path) -> dict:
    """Drop the whole mirror (manifest + views). Salt is preserved so
    pseudonyms stay stable across a rebuild."""
    project = Path(project_path).resolve()
    manifest_n = len(load_manifest(project))
    _save_manifest(project, {})
    return {"manifest_cleared": manifest_n, **gc(project)}
