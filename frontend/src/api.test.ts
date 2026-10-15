// Тесты API-слоя: URL-билдеры, токен, заголовки, разбор ошибок, XHR-пути.
// Запуск: npm test (vitest run). Сеть мокается — сервера не нужно.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  annotationsUrl,
  AuthError,
  bumpWorkspaceNonce,
  clearAuthToken,
  deleteProject,
  downloadFile,
  frameUrl,
  getAuthToken,
  importBackup,
  listPairs,
  projectBackupUrl,
  setAuthToken,
  setTaskName,
  setUnauthorizedHandler,
  startExport,
  taskBackupUrl,
  uploadWorkspace,
  workspaceNonce,
} from "./api";

// --- fetch-мок: очередь ответов + журнал запросов ---
interface SeenRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

let seen: SeenRequest[];
let queued: Array<{
  status: number;
  statusText?: string;
  json?: unknown;
  blob?: Blob;
  headers?: Record<string, string>;
}>;

function mockJsonResponse(entry: (typeof queued)[number]): Response {
  const headers = new Headers(entry.headers);
  if (entry.json !== undefined && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  return {
    ok: entry.status >= 200 && entry.status < 300,
    status: entry.status,
    statusText: entry.statusText ?? "",
    headers,
    json: async () => {
      if (entry.json === undefined) throw new Error("no json");
      return entry.json;
    },
    blob: async () => entry.blob ?? new Blob(["?"]),
    body: null,
  } as unknown as Response;
}

function installFetchMock() {
  seen = [];
  queued = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      seen.push({
        url,
        method: (init.method ?? "GET").toUpperCase(),
        headers: { ...((init.headers as Record<string, string>) ?? {}) },
        body: typeof init.body === "string" ? init.body : null,
      });
      const next = queued.shift();
      if (!next) throw new Error(`незамоканный fetch: ${url}`);
      return mockJsonResponse(next);
    }),
  );
}

// --- XHR-мок: управляемый ответ + ручной прогресс ---
class FakeXHR {
  static last: FakeXHR | null = null;
  method = "";
  url = "";
  headers: Record<string, string> = {};
  status = 0;
  responseText = "";
  upload: { onprogress: ((e: {
    lengthComputable: boolean; loaded: number; total: number;
  }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;

  constructor() {
    FakeXHR.last = this;
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }

  send() {
    // Ответ — наружу: тест вызывает FakeXHR.respond().
  }

  abort() {
    this.onabort?.();
  }

  static respond(status: number, body: unknown) {
    const xhr = FakeXHR.last;
    if (!xhr) throw new Error("нет активного XHR");
    xhr.status = status;
    xhr.responseText = typeof body === "string" ? body : JSON.stringify(body);
    xhr.onload?.();
  }

  static progress(loaded: number, total: number) {
    FakeXHR.last?.upload.onprogress?.({ lengthComputable: true, loaded, total });
  }
}

beforeEach(() => {
  localStorage.clear();
  setUnauthorizedHandler(null);
  FakeXHR.last = null;
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  installFetchMock();
  // jsdom без createObjectURL — стаб для downloadFile.
  if (!URL.createObjectURL) {
    (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL =
      () => "blob:mock";
  }
  if (!URL.revokeObjectURL) {
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL =
      () => {};
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("токен доступа", () => {
  it("roundtrip get/set/clear", () => {
    expect(getAuthToken()).toBeNull();
    setAuthToken("s3cret");
    expect(getAuthToken()).toBe("s3cret");
    clearAuthToken();
    expect(getAuthToken()).toBeNull();
  });

  it("withTokenQuery без токена не трогает URL", async () => {
    const { withTokenQuery } = await import("./api");
    expect(withTokenQuery("/api/v1/x")).toBe("/api/v1/x");
    expect(withTokenQuery("/api/v1/x?a=1")).toBe("/api/v1/x?a=1");
  });

  it("withTokenQuery цепляет и кодирует токен", async () => {
    const { withTokenQuery } = await import("./api");
    setAuthToken("a/b?c=d e");
    expect(withTokenQuery("/api/v1/x")).toBe("/api/v1/x?token=a%2Fb%3Fc%3Dd%20e");
    expect(withTokenQuery("/api/v1/x?a=1")).toBe("/api/v1/x?a=1&token=a%2Fb%3Fc%3Dd%20e");
  });
});

describe("URL-билдеры", () => {
  it("frameUrl: база, video, ver, scale, quality", () => {
    expect(frameUrl("w", 0)).toBe("/api/v1/workspaces/w/frame/0/?video=visualization");
    expect(frameUrl("w с пробелом", 7, "original")).toBe(
      "/api/v1/workspaces/w%20%D1%81%20%D0%BF%D1%80%D0%BE%D0%B1%D0%B5%D0%BB%D0%BE%D0%BC/frame/7/?video=original",
    );
    expect(frameUrl("w", 3, "visualization", "v9", 0.5, 60)).toBe(
      "/api/v1/workspaces/w/frame/3/?video=visualization&v=v9&scale=0.5&quality=60",
    );
    // Дефолты scale=1.0 / quality=78 в URL не попадают.
    expect(frameUrl("w", 3, "visualization", undefined, 1.0, 78)).toBe(
      "/api/v1/workspaces/w/frame/3/?video=visualization",
    );
  });

  it("frameUrl цепляет токен для <img>", () => {
    setAuthToken("t");
    expect(frameUrl("w", 1)).toBe(
      "/api/v1/workspaces/w/frame/1/?video=visualization&token=t",
    );
  });

  it("бэкап/разметка/проект URL + кодирование", () => {
    expect(taskBackupUrl("моя задача")).toBe(
      "/api/v1/workspaces/%D0%BC%D0%BE%D1%8F%20%D0%B7%D0%B0%D0%B4%D0%B0%D1%87%D0%B0/backup",
    );
    expect(annotationsUrl("a")).toBe("/api/v1/pairs/a/annotations");
    expect(annotationsUrl("a", true)).toBe("/api/v1/pairs/a/annotations?media=1");
    expect(projectBackupUrl("p")).toBe("/api/v1/projects/p/backup");
  });
});

describe("nonce workspace", () => {
  it("bumpWorkspaceNonce монотонен и персистентен", () => {
    expect(workspaceNonce("w")).toBe(0);
    expect(bumpWorkspaceNonce("w")).toBe(1);
    expect(bumpWorkspaceNonce("w")).toBe(2);
    expect(workspaceNonce("w")).toBe(2);
    expect(workspaceNonce("other")).toBe(0);
  });
});

describe("fetch-слой", () => {
  it("listPairs: GET + разбор массива", async () => {
    queued.push({ status: 200, json: [{ id: "a" }] });
    await expect(listPairs()).resolves.toEqual([{ id: "a" }]);
    expect(seen[0].url).toBe("/api/v1/workspaces/");
    expect(seen[0].method).toBe("GET");
  });

  it("токен уходит заголовком, без токена — нет", async () => {
    queued.push({ status: 200, json: [] });
    await listPairs();
    expect(seen[0].headers).not.toHaveProperty("Authorization");
    setAuthToken("s3cret");
    queued.push({ status: 200, json: [] });
    await listPairs();
    expect(seen[1].headers.Authorization).toBe("Bearer s3cret");
  });

  it("startExport: force даёт ?force=1", async () => {
    queued.push({ status: 200, json: { state: "done" } });
    await startExport("w", true);
    expect(seen[0].url).toBe("/api/v1/pairs/w/export?force=1");
    expect(seen[0].method).toBe("POST");
  });

  it("deleteProject строит query with_tasks/to", async () => {
    queued.push({ status: 200, json: { deleted: "p" } });
    await deleteProject("пр", "move", "q");
    expect(seen[0].url).toContain("/api/v1/projects/%D0%BF%D1%80/");
    expect(seen[0].url).toContain("with_tasks=move");
    expect(seen[0].url).toContain("to=q");
    expect(seen[0].method).toBe("DELETE");
  });

  it("setTaskName шлёт PATCH {name}, а не {id}", async () => {
    queued.push({ status: 200, json: { id: "w" } });
    await setTaskName("w", "Выезд");
    expect(seen[0].method).toBe("PATCH");
    expect(JSON.parse(seen[0].body ?? "")).toEqual({ name: "Выезд" });
  });

  it("ошибка: приоритет body.error, затем detail, затем statusText", async () => {
    queued.push({ status: 400, statusText: "ST", json: { error: "свое" } });
    await expect(listPairs()).rejects.toThrow("свое");
    queued.push({ status: 400, statusText: "ST", json: { detail: "деталь" } });
    await expect(listPairs()).rejects.toThrow("деталь");
    queued.push({ status: 500, statusText: "ST", json: undefined });
    await expect(listPairs()).rejects.toThrow("ST");
  });

  it("401: AuthError + хендлер разлогина", async () => {
    const seenHandler: string[] = [];
    setUnauthorizedHandler(() => seenHandler.push("logout"));
    queued.push(
      { status: 401, json: { error: "Нужен токен доступа" } },
      { status: 401, json: { error: "Нужен токен доступа" } },
    );
    await expect(listPairs()).rejects.toBeInstanceOf(AuthError);
    await expect(listPairs().catch((e) => (e as Error).message)).resolves.toBe(
      "Нужен токен доступа",
    );
    expect(seenHandler).toEqual(["logout", "logout"]);
  });
});

describe("downloadFile", () => {
  it("стримит чанки с прогрессом и сохраняет blob", async () => {
    const clicks: string[] = [];
    const origCreate = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
      const el = origCreate(tag);
      if (tag === "a") {
        el.click = () => clicks.push((el as HTMLAnchorElement).download);
      }
      return el;
    }) as unknown as typeof document.createElement);

    const seenProgress: number[] = [];
    const chunks = [new Uint8Array([1, 2]), new Uint8Array([3])];
    let i = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ "Content-Length": "3" }),
        body: {
          getReader: () => ({
            read: async () => {
              if (i >= chunks.length) return { done: true, value: undefined };
              return { done: false, value: chunks[i++] };
            },
          }),
        },
      })),
    );
    await downloadFile("/api/v1/x", "f.zip", (f) => {
      if (f !== null) seenProgress.push(f);
    });
    expect(seenProgress).toEqual([2 / 3, 1]);
    expect(clicks).toEqual(["f.zip"]);
  });

  it("без Content-Length — один проход без процентов", async () => {
    const progresses: Array<number | null> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: new Headers(),
        body: null,
        blob: async () => new Blob(["ab"]),
      })),
    );
    await downloadFile("/api/v1/x", "f.zip", (f) => progresses.push(f));
    expect(progresses).toEqual([null]);
  });

  it("401 превращается в AuthError", async () => {
    const calls: number[] = [];
    setUnauthorizedHandler(() => calls.push(1));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 401,
        statusText: "",
        headers: new Headers(),
        json: async () => ({ error: "Нужен токен доступа" }),
      })),
    );
    await expect(downloadFile("/api/v1/x", "f.zip")).rejects.toBeInstanceOf(
      AuthError,
    );
    expect(calls).toEqual([1]);
  });
});

describe("XHR-пути", () => {
  it("uploadWorkspace без файлов — ранний отказ", async () => {
    await expect(uploadWorkspace("n", {})).rejects.toThrow(
      "Выберите хотя бы один видеофайл",
    );
  });

  it("uploadWorkspace: токен в заголовке + разбор успеха", async () => {
    setAuthToken("s3cret");
    const file = new File(["x"], "v.mp4", { type: "video/mp4" });
    const done = uploadWorkspace("n", { source: file });
    expect(FakeXHR.last?.method).toBe("POST");
    expect(FakeXHR.last?.url).toBe("/api/v1/workspaces/");
    expect(FakeXHR.last?.headers.Authorization).toBe("Bearer s3cret");
    FakeXHR.respond(202, { id: "n" });
    await expect(done).resolves.toEqual({ id: "n" });
  });

  it("uploadWorkspace: прогресс в долях", async () => {
    const file = new File(["x"], "v.mp4", { type: "video/mp4" });
    const seen: number[] = [];
    const done = uploadWorkspace("n", { source: file }, (f) => seen.push(f));
    FakeXHR.progress(25, 100);
    FakeXHR.respond(202, { id: "n" });
    await done;
    expect(seen).toEqual([0.25]);
  });

  it("XHR 401 — AuthError и разлогин", async () => {
    const calls: number[] = [];
    setUnauthorizedHandler(() => calls.push(1));
    const file = new File(["x"], "v.mp4", { type: "video/mp4" });
    const done = uploadWorkspace("n", { source: file });
    FakeXHR.respond(401, { error: "Нужен токен доступа" });
    await expect(done).rejects.toBeInstanceOf(AuthError);
    expect(calls).toEqual([1]);
  });

  it("importBackup строит query on_conflict/project", async () => {
    const file = new File(["x"], "b.zip", { type: "application/zip" });
    const done = importBackup(file, { on_conflict: "overwrite", project: "пр" });
    expect(FakeXHR.last?.url).toBe(
      "/api/v1/backups/import?on_conflict=overwrite&project=%D0%BF%D1%80",
    );
    FakeXHR.respond(202, { kind: "backup", id: "w_1" });
    await expect(done).resolves.toEqual({ kind: "backup", id: "w_1" });
  });
});
