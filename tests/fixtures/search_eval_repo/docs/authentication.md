# Authentication

LedgerLite authenticates callers with OAuth2 authorization-code flow.

## Configuring the redirect URI

Set `oauth2.redirect_uri` in `configs/settings.yaml` to the exact callback URL
registered with your identity provider. A mismatch is rejected by the provider
before LedgerLite ever sees the code.

## Token lifetime

Access tokens live for 15 minutes. `OAuth2Client.refresh_token` rotates the
refresh token on every use; the previous refresh token is revoked.

## Session cookies

Browser sessions are stored server side in `SessionStore` and expire after
30 minutes of inactivity.
