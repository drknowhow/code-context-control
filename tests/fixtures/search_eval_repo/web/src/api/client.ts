export interface InvoiceSummary {
  number: string;
  customerId: string;
  total: string;
}

export class ApiClient {
  constructor(private readonly baseUrl: string, private readonly token: string) {}

  async fetchInvoices(): Promise<InvoiceSummary[]> {
    const res = await fetch(`${this.baseUrl}/invoices`, {
      headers: { Authorization: `Bearer ${this.token}` },
    });
    if (!res.ok) throw new Error(`fetchInvoices failed: ${res.status}`);
    return (await res.json()) as InvoiceSummary[];
  }

  async fetchInvoice(number: string): Promise<InvoiceSummary> {
    const res = await fetch(`${this.baseUrl}/invoices/${encodeURIComponent(number)}`);
    return (await res.json()) as InvoiceSummary;
  }
}
