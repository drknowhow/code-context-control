"""Token helpers: hashing and encoding."""
import base64
import hashlib
import secrets


def sha256_digest(data: bytes) -> bytes:
    """Return the raw SHA-256 digest of data."""
    return hashlib.sha256(data).digest()


def base64url_encode(data: bytes) -> str:
    """Encode bytes as unpadded base64url (RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def utf8_decode(data: bytes) -> str:
    """Decode provider bodies leniently; invalid bytes become U+FFFD."""
    return data.decode("utf-8", errors="replace")


def new_code_verifier(length: int = 64) -> str:
    """Generate a PKCE code verifier."""
    return base64url_encode(secrets.token_bytes(length))[:128]
