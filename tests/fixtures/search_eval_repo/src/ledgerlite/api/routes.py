"""HTTP route table."""
from ledgerlite.api.handlers import handle_healthz, handle_invoice_get, handle_invoice_list, handle_token


def register_routes(app) -> None:
    """Attach every handler to the app router."""
    app.add_route("GET", "/healthz", handle_healthz)
    app.add_route("POST", "/oauth/token", handle_token)
    app.add_route("GET", "/invoices", handle_invoice_list)
    app.add_route("GET", "/invoices/{number}", handle_invoice_get)
