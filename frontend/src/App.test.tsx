// Навигация браузера: «Назад» из редактора возвращает к списку, а не
// выбрасывает из приложения. Экран живёт в state, но вход в редактор
// оставляет запись в history — иначе кнопка браузера уводит на пустую
// страницу.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const { pair, detail } = vi.hoisted(() => ({
  pair: {
    id: "ws-test",
    name: "",
    source_name: "source.mp4",
    preview_name: "preview.mp4",
    unassigned_name: null,
    total_frames: 50,
    width: 64,
    height: 48,
    fps: 24,
    source_frames: 50,
    preview_frames: 50,
    source_skipped: 0,
    preview_skipped: 0,
    visible_match: true,
    pair_warning: "",
    fragments: [],
    position: 0,
    updated_at: 0,
    video_ver: "v1",
    quality: 78,
    scale: 0.75,
    indexing: false,
    indexing_progress: null,
    broken: false,
    error: "",
    error_role: null,
    created_at: null,
    last_opened_at: null,
    project_id: null,
    export: null,
  },
  detail: {
    id: "ws-test",
    name: "",
    source_name: "source.mp4",
    preview_name: "preview.mp4",
    unassigned_name: null,
    total_frames: 50,
    width: 64,
    height: 48,
    fps: 24,
    source_frames: 50,
    preview_frames: 50,
    source_skipped: 0,
    preview_skipped: 0,
    visible_match: true,
    pair_warning: "",
    fragments: [],
    position: 0,
    updated_at: 0,
    video_ver: "v1",
    quality: 78,
    scale: 0.75,
    indexing: false,
    indexing_progress: null,
    broken: false,
    error: "",
    error_role: null,
    created_at: null,
    last_opened_at: null,
    project_id: null,
    export: null,
  },
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    authStatus: () => Promise.resolve({ enabled: false }),
    getAuthToken: () => null,
    listPairs: () => Promise.resolve([pair]),
    getWorkspacesStatus: () => Promise.resolve([]),
    getPair: () => Promise.resolve(detail),
    setUnauthorizedHandler: () => {},
  };
});

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

async function mountApp() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<App />);
  });
  await flush();
  return {
    host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

describe("кнопка «Назад» браузера", () => {
  beforeEach(() => {
    window.history.replaceState(null, "");
  });

  it("открытие задачи оставляет запись в истории", async () => {
    const { host, unmount } = await mountApp();
    const row = host.querySelector<HTMLButtonElement>("button.pair-open");
    expect(row).toBeTruthy();
    act(() => {
      row!.click();
    });
    await flush();
    expect(window.history.state).toEqual({ vc: "editor", pairId: "ws-test" });
    unmount();
  });

  it("кнопка «Назад» в редакторе идёт через history", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("button.pair-open")!.click();
    });
    await flush();
    act(() => {
      host.querySelector<HTMLButtonElement>(".editor-back")!.click();
    });
    expect(back).toHaveBeenCalled();
    back.mockRestore();
    unmount();
  });

  it("после «Назад» фокус возвращается на строку задачи", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {
      window.dispatchEvent(
        new PopStateEvent("popstate", { state: { vc: "list" } }),
      );
    });
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("button.pair-open")!.click();
    });
    await flush();
    act(() => {
      host.querySelector<HTMLButtonElement>(".editor-back")!.click();
    });
    await flush();
    expect(document.activeElement).toBe(
      host.querySelector<HTMLButtonElement>("button.pair-open"),
    );
    back.mockRestore();
    unmount();
  });

  it("назад при открытой справке закрывает её, а не редактор", async () => {
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("button.pair-open")!.click();
    });
    await flush();
    act(() => {
      host.querySelector<HTMLButtonElement>('button[aria-label="Справка"]')!.click();
    });
    expect(host.querySelector(".help-panel")).not.toBeNull();
    act(() => {
      window.dispatchEvent(
        new PopStateEvent("popstate", { state: { vc: "list" } }),
      );
    });
    await flush();
    expect(host.querySelector(".help-panel")).toBeNull();
    expect(host.querySelector(".editor")).not.toBeNull();
    expect(window.history.state).toEqual({
      vc: "editor",
      pairId: "ws-test",
    });
    act(() => {
      window.dispatchEvent(
        new PopStateEvent("popstate", { state: { vc: "list" } }),
      );
    });
    await flush();
    expect(host.querySelector(".pair-list")).not.toBeNull();
    unmount();
  });

  it("после перезагрузки в редакторе история начинается со списка", async () => {
    window.history.replaceState({ vc: "editor", pairId: "ws-test" }, "");
    const { unmount } = await mountApp();
    expect(window.history.state).toEqual({ vc: "list" });
    unmount();
  });

  it("popstate со списком возвращает к списку", async () => {
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("button.pair-open")!.click();
    });
    await flush();
    expect(host.querySelector(".editor")).not.toBeNull();
    act(() => {
      window.dispatchEvent(
        new PopStateEvent("popstate", { state: { vc: "list" } }),
      );
    });
    await flush();
    expect(host.querySelector(".editor")).toBeNull();
    expect(host.querySelector(".pair-list")).not.toBeNull();
    unmount();
  });
});
