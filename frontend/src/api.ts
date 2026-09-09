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

export function frameUrl(pairId: string, index: number, kind: "original" | "visualization" = "visualization"): string {
  return `${BASE}/workspaces/${encodeURIComponent(pairId)}/frame/${index}/?video=${kind}`;
}

export function replaceFragments(
  pairId: string,
  fragments: { start: number; end: number; comment?: string }[],
): Promise<{ start: number; end: number; comment?: string }[]> {
  return fetch(`${BASE}/pairs/${encodeURIComponent(pairId)}/fragments/`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fragments),
  }).then((r) => json<{ start: number; end: number; comment?: string }[]>(r));
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
