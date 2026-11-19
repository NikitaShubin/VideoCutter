// Навигация браузера: «Назад» из редактора возвращает к списку, а не
// выбрасывает из приложения. Экран живёт в state, но вход в редактор
// оставляет запись в history — иначе кнопка браузера уводит на пустую
// страницу.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { savePosition } from "./api";
import type { VideoPairDetail } from "./types";

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
    project_id: "proj-1",
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
    fragments: [{ start: 10, end: 20, comment: "" }],
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
    project_id: "proj-1",
    export: null,
  },
}));

/** Управляемые тестом задержки API: глубокие ссылки проверяются на живой
 * загрузке, а не на уже разложенном состоянии. */
const { apiHooks, authHooks } = vi.hoisted(() => ({
  apiHooks: { getPair: null as null | (() => Promise<VideoPairDetail>) },
  authHooks: { enabled: false, token: null as string | null },
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    authStatus: () => Promise.resolve({ enabled: authHooks.enabled }),
    getAuthToken: () => authHooks.token,
    listPairs: () => Promise.resolve([pair]),
    listProjects: () =>
      Promise.resolve([
        {
          id: "proj-1",
          name: "Проект",
          created_at: null,
          model: { id: null, kind: "folder", params: {} },
          task_count: 1,
        },
      ]),
    getWorkspacesStatus: () => Promise.resolve([]),
    getPair: () => (apiHooks.getPair ? apiHooks.getPair() : Promise.resolve(detail)),
    savePosition: vi.fn(() => Promise.resolve({ position: 0 })),
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

async function mountApp(url?: string) {
  // Стартовый адрес задаётся до монтирования: приложение читает URL один раз
  // при загрузке, как настоящая вкладка браузера.
  if (url !== undefined) window.history.replaceState(null, "", url);
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

/** Браузерный «Назад»: адрес уже сменился, затем приходит popstate. */
function popBackTo(url: string): void {
  window.history.replaceState({ vc: "list" }, "", url);
  window.dispatchEvent(
    new PopStateEvent("popstate", { state: { vc: "list" } }),
  );
}

/** Поле номера кадра (1-based, как на экране). */
function frameInput(host: HTMLElement): HTMLInputElement {
  const input = host.querySelector<HTMLInputElement>(".frame-input");
  if (!input) throw new Error("поле номера кадра не найдено");
  return input;
}

/** Управляемый input React'а: setter прототипа + событие input. */
function typeInto(input: HTMLInputElement, text: string): void {
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!;
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("кнопка «Назад» браузера", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
  });

  it("открытие задачи оставляет запись в истории", async () => {
    const { host, unmount } = await mountApp();
    const row = host.querySelector<HTMLButtonElement>("button.pair-open");
    expect(row).toBeTruthy();
    act(() => {
      row!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/tasks/ws-test");
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
      popBackTo("/");
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
      popBackTo("/");
    });
    await flush();
    expect(host.querySelector(".help-panel")).toBeNull();
    expect(host.querySelector(".editor")).not.toBeNull();
    expect(window.history.state).toEqual({
      vc: "editor",
      pairId: "ws-test",
    });
    // После закрытия справки запись редактора возвращена: следующий
    // «Назад» уже уходит к списку.
    expect(window.location.pathname).toBe("/tasks/ws-test");
    act(() => {
      popBackTo("/");
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
      popBackTo("/");
    });
    await flush();
    expect(host.querySelector(".editor")).toBeNull();
    expect(host.querySelector(".pair-list")).not.toBeNull();
    expect(window.location.pathname).toBe("/");
    unmount();
  });
});

describe("адреса экранов (deep links)", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
    authHooks.enabled = false;
    authHooks.token = null;
    apiHooks.getPair = null;
    vi.mocked(savePosition).mockClear();
  });

  it("открывает задачу по /tasks/<id>", async () => {
    const { host, unmount } = await mountApp("/tasks/ws-test");
    expect(host.querySelector(".editor")).not.toBeNull();
    expect(frameInput(host).value).toBe("1");
    unmount();
  });

  it("?frame= — zero-based кадр из адреса", async () => {
    const { host, unmount } = await mountApp("/tasks/ws-test?frame=7");
    expect(frameInput(host).value).toBe("8");
    unmount();
  });

  it("открывает задачу на начале фрагмента", async () => {
    const { host, unmount } = await mountApp("/tasks/ws-test/fragments/1");
    expect(frameInput(host).value).toBe("11");
    unmount();
  });

  it("несуществующий фрагмент: заметка и первый кадр", async () => {
    const { host, unmount } = await mountApp("/tasks/ws-test/fragments/9");
    expect(host.querySelector(".route-notice")?.textContent).toContain(
      "Фрагмент 9",
    );
    expect(frameInput(host).value).toBe("1");
    unmount();
  });

  it("/projects открывает вкладку проектов", async () => {
    const { host, unmount } = await mountApp("/projects");
    expect(
      host.querySelector("#tab-projects")?.getAttribute("aria-selected"),
    ).toBe("true");
    expect(host.querySelector(".project-open")).not.toBeNull();
    unmount();
  });

  it("/projects/<id> открывает задачи с фильтром проекта", async () => {
    const { host, unmount } = await mountApp("/projects/proj-1");
    expect(
      host.querySelector("#tab-tasks")?.getAttribute("aria-selected"),
    ).toBe("true");
    const projectSelect = [...host.querySelectorAll("select")].find((select) =>
      [...select.options].some((option) => option.value === "proj-1"),
    );
    expect(projectSelect?.value).toBe("proj-1");
    expect(host.querySelector("button.pair-open")).not.toBeNull();
    unmount();
  });

  it("вкладки обновляют адрес", async () => {
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("#tab-projects")!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/projects");
    act(() => {
      host.querySelector<HTMLButtonElement>("#tab-tasks")!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/tasks");
    unmount();
  });

  it("клик по проекту кладёт /projects/<id>", async () => {
    const { host, unmount } = await mountApp("/projects");
    await flush();
    act(() => {
      host.querySelector<HTMLButtonElement>(".project-open")!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/projects/proj-1");
    expect(
      host.querySelector("#tab-tasks")?.getAttribute("aria-selected"),
    ).toBe("true");
    unmount();
  });

  it("смена кадра обновляет ?frame= без роста истории", async () => {
    const { host, unmount } = await mountApp("/tasks/ws-test");
    const lengthBefore = window.history.length;
    const input = frameInput(host);
    typeInto(input, "8");
    await flush();
    act(() => {
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    // Адрес обновляется троттлингом (не чаще ~4 раз/с): ждём его окно.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    expect(frameInput(host).value).toBe("8");
    expect(window.location.search).toBe("?frame=7");
    expect(window.history.length).toBe(lengthBefore);
    unmount();
  });

  it("неизвестная задача: заметка и список", async () => {
    const { host, unmount } = await mountApp("/tasks/missing");
    await flush();
    expect(host.querySelector(".editor")).toBeNull();
    expect(host.querySelector(".pair-list")).not.toBeNull();
    expect(host.querySelector(".route-notice")?.textContent).toContain(
      "missing",
    );
    // Адрес не подменяется молча: пользователь видит, что просил.
    expect(window.location.pathname).toBe("/tasks/missing");
    unmount();
  });

  it("неизвестный проект: заметка и все задачи", async () => {
    const { host, unmount } = await mountApp("/projects/missing-project");
    await flush();
    expect(host.querySelector(".route-notice")?.textContent).toContain(
      "missing-project",
    );
    expect(host.querySelector("button.pair-open")).not.toBeNull();
    expect(window.location.pathname).toBe("/projects/missing-project");
    unmount();
  });

  it("возврат из редактора восстанавливает список и адрес", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {
      popBackTo("/");
    });
    const { host, unmount } = await mountApp();
    act(() => {
      host.querySelector<HTMLButtonElement>("button.pair-open")!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/tasks/ws-test");
    act(() => {
      host.querySelector<HTMLButtonElement>(".editor-back")!.click();
    });
    await flush();
    expect(window.location.pathname).toBe("/");
    expect(host.querySelector(".pair-list")).not.toBeNull();
    expect(host.querySelector(".editor")).toBeNull();
    back.mockRestore();
    unmount();
  });

  it("deep link переживает вход по токену", async () => {
    authHooks.enabled = true;
    const { host, unmount } = await mountApp("/tasks/ws-test");
    expect(host.querySelector(".login")).not.toBeNull();
    typeInto(
      host.querySelector<HTMLInputElement>('input[type="password"]')!,
      "secret",
    );
    act(() => {
      host
        .querySelector("form.login")!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    await flush();
    await flush();
    expect(host.querySelector(".editor")).not.toBeNull();
    expect(window.location.pathname).toBe("/tasks/ws-test");
    unmount();
  });

  it("не сохраняет позицию, пока задача не загрузилась", async () => {
    apiHooks.getPair = () => new Promise<VideoPairDetail>(() => {});
    const { unmount } = await mountApp("/tasks/ws-test");
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(vi.mocked(savePosition)).not.toHaveBeenCalled();
    unmount();
  });
});
