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
export function setWorkspaceVideo(
  id: string,
  role: "source" | "preview",
  file: File,
  existing?: "source" | "preview",
): Promise<VideoPair> {
  const form = new FormData();
  form.append("file", file);
  if (existing) form.append("existing", existing);
  return fetch(`${BASE}/workspaces/${encodeURIComponent(id)}/video/${role}/`, {
    method: "POST",
    body: form,
  }).then((r) => json<VideoPair>(r));
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

export function frameUrl(
  pairId: string,
  index: number,
  kind: "original" | "visualization" = "visualization",
  ver?: number,
): string {
  const v = ver !== undefined ? `&v=${ver}` : "";
  return `${BASE}/workspaces/${encodeURIComponent(pairId)}/frame/${index}/?video=${kind}${v}`;
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
