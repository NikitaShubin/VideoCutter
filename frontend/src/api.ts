import type {
  ExportStatus,
  Project,
  VideoPair,
  VideoPairDetail,
} from "./types";

const BASE = "/api/v1";

// ─── Входной фильтр-токен (VC_AUTH_TOKEN) ────────────────────────────────
// Выключен на сервере — токена нет и всё открыто. Включён — токен летит
// заголовком везде, где его можно прицепить (fetch/XHR); для <img> кадров
// и <a download> (экспортные файлы) — query ?token= (см. withTokenQuery).

const TOKEN_KEY = "vc-auth-token";

export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAuthToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* ignore */
  }
}

export function clearAuthToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

function authHeaders(): Record<string, string> {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Прицепить токен query (медиа/скачивание: заголовок не прицепить). */
export function withTokenQuery(url: string): string {
  const token = getAuthToken();
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** 401 от API — токен неверный/протух: App возвращает экран входа. */
export class AuthError extends Error {}

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

function authFailed(detail: string): never {
  unauthorizedHandler?.();
  throw new AuthError(detail);
}

/** fetch с токеном (везде, кроме статуса — он открыт всегда). */
function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const extra = init.headers as Record<string, string> | undefined;
  return globalThis.fetch(url, {
    ...init,
    headers: { ...authHeaders(), ...extra },
  });
}

export function authStatus(): Promise<{ enabled: boolean }> {
  // Статус открыт всегда (секретов не отдаёт) — токен не нужен.
  return apiFetch(`${BASE}/auth/status`).then((r) => json<{ enabled: boolean }>(r));
}

async function json<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    let detail = "Нужен токен доступа";
    try {
      const body = await res.json();
      if (body.error) detail = body.error;
    } catch {
      /* ignore */
    }
    return authFailed(detail);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body.error) detail = body.error;
      else if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export function listPairs(): Promise<VideoPair[]> {
  return apiFetch(`${BASE}/workspaces/`).then((r) => json<VideoPair[]>(r));
}

/** Волатильное состояние задач для тика (без TSV/метрик): готовность,
// прогресс, экспорт, mtime, broken. Клиент мержит в нарисованные строки;
// полный список — на mount/возврат/CRUD и при смене состава. */
export interface WorkspaceStatus {
  id: string;
  indexing: boolean;
  indexing_progress: number | null;
  export: VideoPair["export"];
  updated_at: number;
  broken: boolean;
  error: string;
}

export function getWorkspacesStatus(): Promise<WorkspaceStatus[]> {
  return apiFetch(`${BASE}/workspaces/?light=1`).then((r) =>
    json<WorkspaceStatus[]>(r),
  );
}

export function getPair(id: string): Promise<VideoPairDetail> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`).then((r) =>
    json<VideoPairDetail>(r),
  );
}

// Создаёт workspace загрузкой видео (multipart) с прогрессом.
// ``files`` — одно или два видео: roles "source" и/или "preview".
// Загрузка нескольких файлов XHR пока не поддерживает прогресс по общему телу;
// прогресс считается по первой (и обычно единственной) загрузке.
export interface WorkspaceUploadFiles {
  source?: File;
  preview?: File;
}

export interface UploadOptions {
  /** AbortSignal отмены (ведро на строке во время заливки). */
  signal?: AbortSignal;
  /** Client-uuid приёма (hex32): сервер считает байты, статус — опросом. */
  uploadId?: string;
}

export function uploadWorkspace(
  name: string,
  files: WorkspaceUploadFiles,
  onProgress?: (fraction: number) => void,
  opts: UploadOptions = {},
): Promise<VideoPair> {
  const file = files.source ?? files.preview;
  return new Promise((resolve, reject) => {
    if (!file) {
      reject(new Error("Выберите хотя бы один видеофайл"));
      return;
    }
    const form = new FormData();
    if (files.source) form.append("source", files.source);
    if (files.preview) form.append("preview", files.preview);
    if (name) form.append("name", name);

    const xhr = new XMLHttpRequest();
    const url = opts.uploadId
      ? `${BASE}/workspaces/?upload_id=${encodeURIComponent(opts.uploadId)}`
      : `${BASE}/workspaces/`;
    xhr.open("POST", url);
    xhrAuthHeaders(xhr);
    if (opts.signal) {
      if (opts.signal.aborted) {
        reject(new DOMException("Отменено", "AbortError"));
        return;
      }
      opts.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as VideoPair);
      } else {
        reject(xhrError(xhr, body));
      }
    };
    xhr.onerror = () => reject(new Error("Ошибка сети при загрузке"));
    xhr.onabort = () => reject(new DOMException("Отменено", "AbortError"));
    xhr.send(form);
  });
}

export interface UploadReceipt {
  received: number;
  total: number;
}

export async function getUploadStatus(uploadId: string): Promise<UploadReceipt | null> {
  // 404 = неизвестно/готово — считать готовым (бар не врёт назад).
  const res = await apiFetch(
    `${BASE}/uploads/${encodeURIComponent(uploadId)}/status`);
  if (!res.ok) return null;
  return res.json() as Promise<UploadReceipt>;
}

// Добавляет/заменяет файл роли (источник/превью). При ``existing`` заодно
// назначает прежний «нейтральный» файл роли existing (сценарий «был один файл»).
// Прогресс — через XHR (upload.onprogress), процент реального тела.
export function setWorkspaceVideo(
  id: string,
  role: "source" | "preview",
  file: File,
  existing?: "source" | "preview",
  onProgress?: (fraction: number) => void,
): Promise<VideoPair> {
  const form = new FormData();
  form.append("file", file);
  if (existing) form.append("existing", existing);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`);
    xhrAuthHeaders(xhr);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as VideoPair);
      } else {
        reject(xhrError(xhr, body));
      }
    };
    xhr.onerror = () => reject(new Error("Ошибка сети при загрузке"));
    xhr.send(form);
  });
}

// Назначает роль уже загруженному «неразмеченному» видеофайлу (unassigned).
export function assignWorkspaceVideo(
  id: string,
  role: "source" | "preview",
  filename: string,
): Promise<VideoPair> {
  const form = new FormData();
  form.append("assign", filename);
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`, {
    method: "POST",
    body: form,
  }).then((r) => json<VideoPair>(r));
}

// Убирает ролевой файл (нельзя удалить единственное видео).
export function removeWorkspaceVideo(
  id: string,
  role: "source" | "preview",
): Promise<VideoPair> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`, {
    method: "DELETE",
  }).then((r) => json<VideoPair>(r));
}

// Меняет роли двух видео местами (source <-> preview).
export function swapVideos(id: string): Promise<VideoPair> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/swap/`, {
    method: "POST",
  }).then((r) => json<VideoPair>(r));
}

// Безвозвратно удаляет workspace со всеми данными.
export function deleteWorkspace(id: string): Promise<{ deleted: string }> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`, {
    method: "DELETE",
  }).then((r) => json<{ deleted: string }>(r));
}

// Переименовывает папку workspace (id задачи). Дубли id невозможны.
export function renameWorkspace(id: string, name: string): Promise<VideoPair> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: name }),
  }).then((r) => json<VideoPair>(r));
}

// Отображаемое имя задачи (как task.name в CVAT): дубли разрешены,
// папка не двигается — блокировки на индексацию не нужно.
export function setTaskName(id: string, name: string): Promise<VideoPair> {
  return apiFetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then((r) => json<VideoPair>(r));
}

// ─── Проекты ────────────────────────────────────────────────────────────────

export function listProjects(): Promise<Project[]> {
  return apiFetch(`${BASE}/projects/`).then((r) => json<Project[]>(r));
}

export function createProject(name: string): Promise<Project> {
  return apiFetch(`${BASE}/projects/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then((r) => json<Project>(r));
}

export function renameProject(id: string, name: string): Promise<Project> {
  return apiFetch(`${BASE}/projects/${encodeURIComponent(id)}/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then((r) => json<Project>(r));
}

/** Как удалить проект: keep — задачи остаются, cascade — вместе с задачами. */
export function deleteProject(
  id: string,
  withTasks: "keep" | "cascade" | "move",
  to?: string,
): Promise<{ deleted: string; tasks: Record<string, unknown> }> {
  const qs = new URLSearchParams({ with_tasks: withTasks });
  if (to) qs.set("to", to);
  return apiFetch(`${BASE}/projects/${encodeURIComponent(id)}/?${qs}`, {
    method: "DELETE",
  }).then((r) => json<{ deleted: string; tasks: Record<string, unknown> }>(r));
}

export function attachTask(
  projectId: string,
  taskId: string,
): Promise<{ task_id: string; project_id: string }> {
  return apiFetch(`${BASE}/projects/${encodeURIComponent(projectId)}/tasks/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  }).then((r) => json<{ task_id: string; project_id: string }>(r));
}

export function detachTask(
  projectId: string,
  taskId: string,
): Promise<{ task_id: string; project_id: null }> {
  return apiFetch(
    `${BASE}/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/`,
    { method: "DELETE" },
  ).then((r) => json<{ task_id: string; project_id: null }>(r));
}

// Сохраняет настройки просмотра (ползунки качества/масштаба).
export function setPairSettings(
  pairId: string,
  quality: number,
  scale: number,
): Promise<{ quality: number; scale: number }> {
  return apiFetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quality, scale }),
  }).then((r) => json<{ quality: number; scale: number }>(r));
}

export interface CacheState {
  caps: { gops: number; mb: number };
  usage: {
    gops: number;
    gops_cap: number;
    mb: number;
    mb_cap: number;
    sources: number;
    per_source?: Record<string, { gops: number; mb: number }>;
  };
  tuning: {
    ncpu: number;
    ram_mb: number;
    auto: Record<string, number>;
    source: Record<string, string>;
  };
}

export function getCache(): Promise<CacheState> {
  return apiFetch(`${BASE}/cache`).then((r) => json<CacheState>(r));
}

export function setCache(patch: {
  gops?: number;
  mb?: number;
}): Promise<CacheState> {
  return apiFetch(`${BASE}/cache`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  }).then((r) => json<CacheState>(r));
}

export function frameUrl(
  pairId: string,
  index: number,
  kind: "original" | "visualization" = "visualization",
  ver?: string,
  scale?: number,
  quality?: number,
): string {
  let url = `${BASE}/workspaces/${encodeURIComponent(pairId)}/frame/${index}/?video=${kind}`;
  if (ver) url += `&v=${encodeURIComponent(ver)}`;
  if (scale !== undefined && scale < 1.0) url += `&scale=${scale}`;
  if (quality !== undefined && quality !== 78) url += `&quality=${quality}`;
  // <img> заголовок не несёт — токен query (см. withTokenQuery).
  return withTokenQuery(url);
}

// Персистентный nonce для инвалидации кэша при удалении задачи (localStorage).
// Нужен, чтобы браузер не отдавал JPEG по старым URL даже после F5/перезагрузки.
// Хранится отдельно для каждого workspace.
export function workspaceNonce(id: string): number {
  try {
    return parseInt(localStorage.getItem(`vc_nonce:${id}`) || "0", 10) || 0;
  } catch {
    return 0;
  }
}

export function bumpWorkspaceNonce(id: string): number {
  const next = workspaceNonce(id) + 1;
  try {
    localStorage.setItem(`vc_nonce:${id}`, String(next));
  } catch {
    /* ignore */
  }
  return next;
}

export function replaceFragments(
  pairId: string,
  fragments: { start: number; end: number; comment?: string }[],
  position: number,
): Promise<{ start: number; end: number; comment?: string }[]> {
  return apiFetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/fragments/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fragments, position }),
  }).then((r) => json<{ start: number; end: number; comment?: string }[]>(r));
}

export function savePosition(
  pairId: string,
  position: number,
  keepalive = false,
): Promise<{ position: number }> {
  return apiFetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/position`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ position }),
    keepalive,
  }).then((r) => json<{ position: number }>(r));
}

export function startExport(pairId: string, force = false): Promise<ExportStatus> {
  const url = `${BASE}/pairs/${encodeURIComponent(pairId)}/export${force ? "?force=1" : ""}`;
  return apiFetch(url, { method: "POST" }).then((r) => json<ExportStatus>(r));
}

export function getExportStatus(pairId: string): Promise<ExportStatus> {
  return apiFetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/export/status`).then((r) =>
    json<ExportStatus>(r),
  );
}

export function cancelExport(pairId: string): Promise<ExportStatus> {
  return apiFetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/export/cancel`, { method: "POST" }).then((r) =>
    json<ExportStatus>(r),
  );
}

// ─── Бэкап/восстановление и обмен разметкой (P3) ────────────────────────────
// Скачивание — обычной ссылкой <a href download> (GET отдаёт zip потоком).
// Заливка — через XHR (upload.onprogress), как остальные загрузки.

export function taskBackupUrl(id: string): string {
  return `${BASE}/workspaces/${encodeURIComponent(id)}/backup`;
}

export function annotationsUrl(id: string, withMedia = false): string {
  const base = `${BASE}/pairs/${encodeURIComponent(id)}/annotations`;
  return withMedia ? `${base}?media=1` : base;
}

export function projectBackupUrl(id: string): string {
  return `${BASE}/projects/${encodeURIComponent(id)}/backup`;
}

/** Ответ POST /backups/import (202): задача — {id, project_id}, проект — {id, tasks}. */
export interface BackupImportResult {
  kind: string;
  id: string;
  project_id?: string | null;
  tasks?: string[];
}

/** Ответ POST .../annotations/import (200): счётчики применённой разметки. */
export interface AnnotationsImportResult {
  task_id: string;
  fragments: number;
  position: number;
}

/** Заголовок токена для XHR (fetch идёт через apiFetch). */
function xhrAuthHeaders(xhr: XMLHttpRequest): void {
  const token = getAuthToken();
  if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
}

/** Ошибка XHR-ответа; 401 заодно сбрасывает на экран входа. */
function xhrError(xhr: XMLHttpRequest, body: unknown): Error {
  const err = body as { error?: string } | null;
  const detail = err?.error ?? `HTTP ${xhr.status}`;
  if (xhr.status === 401) {
    unauthorizedHandler?.();
    return new AuthError(detail);
  }
  return new Error(detail);
}

function xhrPostFile<T>(
  url: string,
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhrAuthHeaders(xhr);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
      } else {
        reject(xhrError(xhr, body));
      }
    };
    xhr.onerror = () => reject(new Error("Ошибка сети при загрузке"));
    xhr.send(form);
  });
}

export interface ImportBackupOptions {
  on_conflict?: "error" | "rename" | "overwrite";
  project?: string;
  onProgress?: (fraction: number) => void;
}

// Восстановление из бэкапа (ответ 202, индексация — в фоне; список опросить).
// Без on_conflict — default rename: совпадающие имена получают суффикс _1.
export function importBackup(
  file: File,
  opts: ImportBackupOptions = {},
): Promise<BackupImportResult> {
  const qs = new URLSearchParams();
  if (opts.on_conflict) qs.set("on_conflict", opts.on_conflict);
  if (opts.project) qs.set("project", opts.project);
  const q = qs.toString();
  return xhrPostFile<BackupImportResult>(
    `${BASE}/backups/import${q ? `?${q}` : ""}`,
    file,
    opts.onProgress,
  );
}

export function importAnnotations(
  pairId: string,
  file: File,
  opts: { force?: boolean; onProgress?: (fraction: number) => void } = {},
): Promise<AnnotationsImportResult> {
  const q = opts.force ? "?force=1" : "";
  return xhrPostFile<AnnotationsImportResult>(
    `${BASE}/pairs/${encodeURIComponent(pairId)}/annotations/import${q}`,
    file,
    opts.onProgress,
  );
}

// Скачивание файла с откликом: бэкапы/разметка собираются на сервере
// синхронно целиком до первого байта — голая ссылка висела бы мёртвой
// всё время сборки. Сначала спиннер (заголовков ещё нет), затем % по
// Content-Length. Замечание: файл держится в памяти — для локального
// контура с умеренными объёмами приемлемо.
export function downloadFile(
  url: string,
  filename: string,
  onProgress?: (fraction: number | null) => void,
  headers: Record<string, string> = authHeaders(),
): Promise<void> {
  const fail = async (res: Response): Promise<never> => {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body.error) detail = body.error;
    } catch {
      /* ignore */
    }
    if (res.status === 401) return authFailed(detail);
    throw new Error(detail);
  };
  const save = (blob: Blob) => {
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(href), 5000);
  };
  return apiFetch(url, headers ? { headers } : undefined).then(async (res) => {
    if (!res.ok) return fail(res);
    const total = Number(res.headers.get("Content-Length")) || 0;
    if (!res.body || !total) {
      onProgress?.(null);
      save(await res.blob());
      return;
    }
    const reader = res.body.getReader();
    const chunks: BlobPart[] = [];
    let loaded = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      onProgress?.(loaded / total);
    }
    save(new Blob(chunks));
  });
}
