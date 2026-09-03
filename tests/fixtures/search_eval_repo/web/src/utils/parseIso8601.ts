export function parseIso8601(value: string): Date {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error(`invalid ISO 8601 timestamp: ${value}`);
  return date;
}
