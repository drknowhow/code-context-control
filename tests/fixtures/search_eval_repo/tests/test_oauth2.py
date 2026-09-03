from ledgerlite.auth.oauth2 import OAuth2Client, OAuth2Error


class FakeTransport(OAuth2Client):
    def __init__(self, response):
        super().__init__("id", "secret", "https://idp.example", "https://app.example/cb")
        self._response = response

    def _post(self, url, payload):
        return dict(self._response)


class TestOAuth2Client:
    def test_exchange_code(self):
        client = FakeTransport({"access_token": "a", "refresh_token": "r"})
        assert client.exchange_code("code", "verifier")["access_token"] == "a"

    def test_exchange_code_error(self):
        client = FakeTransport({"error": "invalid_grant"})
        try:
            client.exchange_code("code", "verifier")
        except OAuth2Error as exc:
            assert "invalid_grant" in str(exc)

    def test_refresh_token_rotates(self):
        client = FakeTransport({"access_token": "a", "refresh_token": "r2"})
        assert client.refresh_token("r1")["refresh_token"] == "r2"
