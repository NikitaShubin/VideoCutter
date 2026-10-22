import type { DefectRole, VideoPair } from "../types";

/** Сломана ли роль задачи (по данным бэкенда: error_role). */
export function isRoleBroken(
  role: DefectRole | null | undefined,
  which: "source" | "preview",
): boolean {
  if (!role) return false;
  return role === "both" || role === which;
}

/** Короткая пометка рядом с именем файла: файл не читается.
 *
 * Короткая — чтобы ряд с именем и кнопками не ломался переносом; полная
 * причина (текст ошибки) — в подсказке по имени файла.
 */
export function defectHint(role: DefectRole | null | undefined): string {
  return role ? "⚠ не читается" : "";
}

/** Битые задачи по проектам: id проекта -> имена задач (для подсветки). */
export function projectDefects(
  pairs: VideoPair[] | null | undefined,
): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const p of pairs ?? []) {
    if (!p.broken || p.project_id == null) continue;
    const names = out.get(p.project_id) ?? [];
    names.push(p.name || p.id);
    out.set(p.project_id, names);
  }
  return out;
}
