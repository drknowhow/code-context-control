from ledgerlite.auth.tokens import base64url_encode, sha256_digest, utf8_decode


def test_sha256_digest_length():
    assert len(sha256_digest(b"abc")) == 32


def test_base64url_encode_has_no_padding():
    assert "=" not in base64url_encode(b"\x00\x01")


def test_utf8_decode_replaces_invalid_bytes():
    assert utf8_decode(b"\xff") == "\ufffd"
