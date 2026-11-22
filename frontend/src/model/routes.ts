/** Адреса экранов VideoCutter — в духе CVAT.
 *
 * Сервер отдаёт index.html на любой не-API путь (frontend/default.conf),
 * поэтому весь маршрут живёт в браузере через History API, без роутера и
 * без новых зависимостей:
 *
 *   /                          список, вкладка и фильтр — из localStorage
 *   /tasks                     задачи, все проекты
 *   /projects                  проекты
 *   /operations                фоновые операции
 *   /projects/<id>             задачи, отфильтрованные по проекту (drill-down)
 *   /tasks/<id>                редактор задачи
 *   /tasks/<id>/fragments/<n>  редактор на начале фрагмента n (нумерация 1-based,
 *                              как в интерфейсе)
 *   ?frame=<n>                 точный кадр (ZERO-BASED, как в CVAT: 0 — первый)
 *
 * `?frame=` сильнее фрагмента. Значение клампится уже в редакторе, когда
 * известно total_frames. Отрицательные и слишком большие числа — не повод
 * падать: это тот же кадр после клампа.
 */

export type Tab = "tasks" | "projects" | "operations";

/** Значения фильтра проекта повторяют селект списка (см. PairList). */
export const PROJECT_ALL = "__all__";
export const PROJECT_NONE = "__none__";

export interface RouteState {
  screen: "list" | "editor";
  /** id задачи; null — экран списка. */
  pairId: string | null;
  /** null — адрес `/`: вкладка берётся из localStorage. */
  tab: Tab | null;
  /** null — все проекты; PROJECT_NONE — без проекта; иначе id проекта. */
  project: string | null;
  /** Точный кадр из ?frame= (zero-based). null — в адресе не задан. */
  frame: number | null;
  /** Начало фрагмента из пути (1-based). null — в адресе не задан. */
  fragment: number | null;
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function segments(pathname: string): string[] {
  return pathname.split("/").filter(Boolean).map(decodeSegment);
}

/** ?frame= — целое zero-based; мусор игнорируем, диапазон проверяет редактор. */
function parseFrame(raw: string | null): number | null {
  if (raw === null) return null;
  const text = raw.trim();
  if (!/^-?\d+$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) ? value : null;
}

/** Номер фрагмента: 0 у нечислового значения гарантирует заметку «не найден». */
function parseFragment(raw: string): number {
  if (!/^\d+$/.test(raw)) return 0;
  const value = Number(raw);
  return Number.isSafeInteger(value) ? value : 0;
}

export function parseRoute(pathname: string, search: string): RouteState {
  const parts = segments(pathname || "/");
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const frame = parseFrame(params.get("frame"));
  const list = (tab: Tab, project: string | null): RouteState => ({
    screen: "list",
    pairId: null,
    tab,
    project,
    frame,
    fragment: null,
  });

  if (parts[0] === "tasks" && parts.length >= 2) {
    return {
      screen: "editor",
      pairId: parts[1],
      tab: null,
      project: null,
      frame,
      fragment:
        parts[2] === "fragments" && parts.length >= 4
          ? parseFragment(parts[3])
          : null,
    };
  }
  if (parts[0] === "tasks") {
    // ?project=none — расширение адреса для фильтра «Без проекта».
    return list("tasks", params.get("project") === "none" ? PROJECT_NONE : null);
  }
  if (parts[0] === "projects") {
    return parts.length === 1
      ? list("projects", null)
      : list("tasks", parts[1]);
  }
  if (parts[0] === "operations") {
    return list("operations", null);
  }
  // `/` — единственный адрес без собственного решения о вкладке.
  if (parts.length === 0) return { ...list("tasks", null), tab: null };
  // Незнакомый путь не ломает приложение: показываем задачи.
  return list("tasks", null);
}

export function taskPath(id: string): string {
  return `/tasks/${encodeURIComponent(id)}`;
}

export function projectPath(id: string): string {
  return `/projects/${encodeURIComponent(id)}`;
}

/** Адрес вкладки списка. */
export function listPath(tab: Tab): string {
  if (tab === "projects") return "/projects";
  if (tab === "operations") return "/operations";
  return "/tasks";
}

/** Адрес выбранного значения фильтра проекта. */
export function projectFilterPath(filter: string): string {
  if (filter === PROJECT_NONE) return "/tasks?project=none";
  if (!filter || filter === PROJECT_ALL) return "/tasks";
  return projectPath(filter);
}

/** Текущий кадр в адресе: pathname и чужие параметры сохраняются. */
export function withFrame(
  pathname: string,
  search: string,
  frame: number,
): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.set("frame", String(frame));
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ""}`;
}
