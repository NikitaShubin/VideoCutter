import type {
  ExportStatus,
  VideoPair,
  VideoPairDetail,
} from "./types";

const BASE = "/api/v1";

async function json<T>(res: Response): Promise<T> {
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
  return fetch(`${BASE}/workspaces/`).then((r) => json<VideoPair[]>(r));
}

export function getPair(id: string): Promise<VideoPairDetail> {
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`).then((r) =>
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

export function uploadWorkspace(
  name: string,
  files: WorkspaceUploadFiles,
  onProgress?: (fraction: number) => void,
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
    xhr.open("POST", `${BASE}/workspaces/`);
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
        const err = body as { error?: string } | null;
        reject(new Error(err?.error ?? `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("Ошибка сети при загрузке"));
    xhr.send(form);
  });
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
        const err = body as { error?: string } | null;
        reject(new Error(err?.error ?? `HTTP ${xhr.status}`));
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
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`, {
    method: "POST",
    body: form,
  }).then((r) => json<VideoPair>(r));
}

// Убирает ролевой файл (нельзя удалить единственное видео).
export function removeWorkspaceVideo(
  id: string,
  role: "source" | "preview",
): Promise<VideoPair> {
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`, {
    method: "DELETE",
  }).then((r) => json<VideoPair>(r));
}

// Меняет роли двух видео местами (source <-> preview).
export function swapVideos(id: string): Promise<VideoPair> {
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/swap/`, {
    method: "POST",
  }).then((r) => json<VideoPair>(r));
}

// Безвозвратно удаляет workspace со всеми данными.
export function deleteWorkspace(id: string): Promise<{ deleted: string }> {
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`, {
    method: "DELETE",
  }).then((r) => json<{ deleted: string }>(r));
}

// Переименовывает workspace (id задачи).
export function renameWorkspace(id: string, name: string): Promise<VideoPair> {
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }).then((r) => json<VideoPair>(r));
}

// Сохраняет настройки просмотра (ползунки качества/масштаба).
export function setPairSettings(
  pairId: string,
  quality: number,
  scale: number,
): Promise<{ quality: number; scale: number }> {
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ quality, scale }),
  }).then((r) => json<{ quality: number; scale: number }>(r));
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
  return url;
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
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/fragments/`, {
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
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/position`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ position }),
    keepalive,
  }).then((r) => json<{ position: number }>(r));
}

export function startExport(pairId: string): Promise<ExportStatus> {
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/export`, { method: "POST" }).then((r) =>
    json<ExportStatus>(r),
  );
}

export function getExportStatus(pairId: string): Promise<ExportStatus> {
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/export/status`).then((r) =>
    json<ExportStatus>(r),
  );
}
