export function formatCurrency(amount: number, currency: string, locale = "en-US"): string {
  const formatter = new Intl.NumberFormat(locale, { style: "currency", currency });
  return formatter.format(amount);
}
