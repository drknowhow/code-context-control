"""OAuth2 authorization-code client."""
import time

from ledgerlite.auth.tokens import base64url_encode, sha256_digest
from ledgerlite.utils.retry import retry_with_backoff


class OAuth2Error(Exception):
    """Raised when the identity provider rejects a request."""


class OAuth2Client:
    """Exchange authorization codes for tokens and rotate refresh tokens."""

    def __init__(self, client_id: str, client_secret: str, token_url: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self._http2_enabled = False

    def authorization_url(self, state: str, code_verifier: str) -> str:
        challenge = base64url_encode(sha256_digest(code_verifier.encode("utf-8")))
        return (f"{self.token_url}/authorize?client_id={self.client_id}"
                f"&redirect_uri={self.redirect_uri}&state={state}"
                f"&code_challenge={challenge}&code_challenge_method=S256")

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        """Exchange an authorization code for an access token and refresh token."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        response = retry_with_backoff(lambda: self._post(self.token_url, payload), attempts=3)
        if "access_token" not in response:
            raise OAuth2Error(response.get("error", "unknown error"))
        response["obtained_at"] = time.time()
        return response

    def refresh_token(self, refresh_token: str) -> dict:
        """Rotate the refresh token: the old one is revoked by the provider."""
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        response = self._post(self.token_url, payload)
        if "refresh_token" not in response:
            raise OAuth2Error("provider did not rotate the refresh token")
        return response

    def _post(self, url: str, payload: dict) -> dict:
        raise NotImplementedError("transport is injected in tests")
