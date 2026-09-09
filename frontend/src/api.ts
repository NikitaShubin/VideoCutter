import type {
  ExportResponse,
  Fragment,
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
  return fetch(`${BASE}/pairs/`).then((r) => json<VideoPair[]>(r));
}

export function getPair(id: number): Promise<VideoPairDetail> {
  return fetch(`${BASE}/pairs/${id}/`).then((r) => json<VideoPairDetail>(r));
}

export function createPair(
  original: File,
  visualization: File | null,
): Promise<VideoPairDetail> {
  const fd = new FormData();
  fd.append("original", original);
  if (visualization) fd.append("visualization", visualization);
  return fetch(`${BASE}/pairs/`, { method: "POST", body: fd }).then((r) =>
    json<VideoPairDetail>(r),
  );
}

export function frameUrl(pairId: number, index: number, kind: "original" | "visualization" = "visualization"): string {
  return `${BASE}/pairs/${pairId}/frame/${index}/?video=${kind}`;
}

export function listFragments(pairId: number): Promise<Fragment[]> {
  return fetch(`${BASE}/pairs/${pairId}/fragments/`).then((r) =>
    json<Fragment[]>(r),
  );
}

export function replaceFragments(
  pairId: number,
  fragments: { start: number; end: number; comment?: string }[],
): Promise<Fragment[]> {
  return fetch(`${BASE}/pairs/${pairId}/fragments/replace`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fragments),
  }).then((r) => json<Fragment[]>(r));
}

export function exportFragments(pairId: number): Promise<ExportResponse> {
  return fetch(`${BASE}/pairs/${pairId}/export`, { method: "POST" }).then((r) =>
    json<ExportResponse>(r),
  );
}