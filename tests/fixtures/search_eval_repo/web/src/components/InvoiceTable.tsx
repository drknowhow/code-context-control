import { InvoiceSummary } from "../api/client";
import { formatCurrency } from "../utils/formatCurrency";

export interface InvoiceTableProps {
  invoices: InvoiceSummary[];
  currency: string;
}

export function InvoiceTable({ invoices, currency }: InvoiceTableProps) {
  return (
    <table>
      <tbody>
        {invoices.map((inv) => (
          <tr key={inv.number}>
            <td>{inv.number}</td>
            <td>{inv.customerId}</td>
            <td>{formatCurrency(Number(inv.total), currency)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
