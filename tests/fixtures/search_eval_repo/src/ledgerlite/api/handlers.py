"""Request handlers."""


def handle_healthz(request):
    return {"status": "ok"}


def handle_token(request):
    client = request.app.oauth2_client
    return client.exchange_code(request.form["code"], request.form["code_verifier"])


def handle_invoice_list(request):
    store = request.app.store
    return [row for row in store.conn.execute("SELECT number FROM invoices")]


def handle_invoice_get(request):
    row = request.app.store.get_invoice(request.path_params["number"])
    if row is None:
        return {"error": "not found"}, 404
    return {"number": row[0], "customer_id": row[1], "total": row[2]}
