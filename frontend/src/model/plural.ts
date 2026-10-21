/** Русские числительные для списков.
 *
 * pluralCount(5, "задача", "задачи", "задач") → "5 задач".
 * pluralWord(1, "кадра", "кадров", "кадров") → "кадра" (родительный для «из N …»).
 */
export function pluralCount(
  n: number,
  one: string,
  few: string,
  many: string,
): string {
  return `${n} ${pluralWord(n, one, few, many)}`;
}

/** Только слово формы (без числа): 1 → one, 2-4 → few, остальное → many. */
export function pluralWord(
  n: number,
  one: string,
  few: string,
  many: string,
): string {
  const abs = Math.abs(Math.trunc(n));
  const hundreds = abs % 100;
  const tens = hundreds % 10;
  if (hundreds >= 11 && hundreds <= 14) return many;
  if (tens === 1) return one;
  if (tens >= 2 && tens <= 4) return few;
  return many;
}
