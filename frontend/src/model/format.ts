// Единый русский формат чисел: десятичная запятая, неразрывный пробел
// в тысячах и перед знаком процента. Живёт отдельно от компонентов, чтобы
// список, редактор и статистика не расходились в написании.

export function formatNumber(value: number, fractionDigits = 0): string {
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatPercent(value: number, fractionDigits = 0): string {
  return `${formatNumber(value, fractionDigits)} %`;
}
