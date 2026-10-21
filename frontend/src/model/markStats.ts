/** Статистика выделенных кадров: сколько выделено и какая это доля.
 *
 * Чистый TS-модуль без React. Считает по тем же правилам, что редактор
 * (границы фрагмента включительные), и переиспользуется в списках задач
 * и проектов — одна формула на все экраны.
 */
import type { Fragment } from "../types";
import { pluralWord } from "./plural";

export interface MarkStats {
  /** Выделено кадров (сумма длин фрагментов). */
  marked: number;
  /** Всего кадров (0 — общее число неизвестно). */
  total: number;
  /** Доля выделенных, % (null — общее число кадров неизвестно). */
  percent: number | null;
}

/** Статистика одной задачи: по фрагментам и общему числу кадров. */
export function markedFrameStats(
  fragments: Fragment[] | null | undefined,
  totalFrames: number | null | undefined,
): MarkStats {
  let marked = 0;
  for (const f of fragments ?? []) {
    const len = f.end - f.start + 1;
    if (len > 0) marked += len;
  }
  const total =
    typeof totalFrames === "number" && totalFrames > 0 ? totalFrames : 0;
  return { marked, total, percent: percentOf(marked, total) };
}

/** Статистика по набору задач (проект): суммы и доля от суммы кадров. */
export function aggregateMarkStats(items: MarkStats[]): MarkStats {
  let marked = 0;
  let total = 0;
  for (const s of items) {
    marked += s.marked;
    total += s.total;
  }
  return { marked, total, percent: percentOf(marked, total) };
}

/** Подпись для списков: «выделено 20 из 100 кадров (20.0%)»; пусто — выделенного нет. */
export function markedLabel(stats: MarkStats): string {
  if (stats.marked <= 0 || stats.percent === null) return "";
  // После «из» — родительный: 1 кадра, 2-4 кадра, 5+ кадров.
  const word = pluralWord(stats.total, "кадра", "кадра", "кадров");
  return `выделено ${stats.marked} из ${stats.total} ${word} (${stats.percent.toFixed(1)}%)`;
}

function percentOf(marked: number, total: number): number | null {
  if (total <= 0) return null;
  return Math.round((100 * marked / total) * 10) / 10;
}
