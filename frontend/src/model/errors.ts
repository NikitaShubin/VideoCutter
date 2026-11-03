// Причина сбоя для человека, а не для журнала: сетевые ошибки браузера
// («Failed to fetch») ничего не говорят пользователю. Всё остальное
// оставляем как есть — сообщения сервера обычно содержат суть.
export function failureReason(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error ?? "");
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(text)) {
    return "нет связи с сервером";
  }
  if (/abort/i.test(text)) return "запрос отменён";
  return text || "неизвестная ошибка";
}
