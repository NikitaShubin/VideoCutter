// Регрессии редактора на медленном сервере: экран не «залипает» навечно,
// ошибка кадра не крадёт кольцо ожидания, серия тапов — прыжок (без
// «анимации» промкадров), удержание стрелки — стрим с дропом.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CutEditor } from "./CutEditor";
import type { VideoPairDetail } from "../types";
import {
  getCache,
  replaceFragments,
  startExport,
  type CacheState,
} from "../api";

let resolvePair: (p: VideoPairDetail) => void = () => {};
let pairDetail: VideoPairDetail;

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getPair: () =>
      new Promise<VideoPairDetail>((res) => {
        resolvePair = res;
      }),
    getCache: vi.fn(() =>
      Promise.resolve({
        caps: { gops: 0, mb: 0 },
        usage: {
          gops: 0,
          gops_cap: 0,
          mb: 0,
          mb_cap: 0,
          sources: 0,
        },
        tuning: { ncpu: 0, ram_mb: 0, auto: {}, source: {} },
      } as CacheState),
    ),
    setPairSettings: () => Promise.resolve().then(() => undefined),
    replaceFragments: vi.fn(() => Promise.resolve()),
    startExport: vi.fn(() =>
      Promise.resolve({
        state: "done" as const,
        files: [
          {
            index: 0,
            filename: "frag_0001.mp4",
            url: "/export/frag_0001.mp4",
          },
        ],
      }),
    ),
    getExportStatus: vi.fn(() => Promise.resolve({ state: "idle" as const })),
  };
});

function oneFrame(total: number, position: number): VideoPairDetail {
  return {
    id: "ws-test",
    name: "",
    source_name: "source.mp4",
    preview_name: "preview.mp4",
    unassigned_name: null,
    total_frames: total,
    width: 64,
    height: 48,
    fps: 24,
    source_frames: total,
    preview_frames: total,
    source_skipped: 0,
    preview_skipped: 0,
    visible_match: true,
    pair_warning: "",
    fragments: [],
    position,
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
  };
}

/** Кадр, который запросил загрузчик, из URL вида /frame/N/. */
function frameIdx(src: string): number {
  const m = /\/frame\/(\d+)\//.exec(src);
  return m ? parseInt(m[1], 10) : -1;
}

class FakeImage {
  static issued: FakeImage[] = [];
  src = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() {
    FakeImage.issued.push(this);
  }

  static targets(): number[] {
    return FakeImage.issued.map((im) => frameIdx(im.src));
  }

  static last(): FakeImage {
    return FakeImage.issued[FakeImage.issued.length - 1];
  }
}

async function mountEditor(onBack: () => void = () => {}) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<CutEditor pairId="ws-test" onBack={onBack} />);
  });
  await act(async () => {
    resolvePair(pairDetail);
  });
  return {
    host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

function tap(code: string, repeat = false): void {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keydown", { code, repeat }));
  });
}

function release(code: string): void {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keyup", { code }));
  });
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

function keyPress(input: HTMLInputElement, key: string): void {
  act(() => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

/** Управляемый textarea React'а: setter прототипа + событие input. */
function typeIntoArea(area: HTMLTextAreaElement, text: string): void {
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )!.set!;
    setter.call(area, text);
    area.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/** Сенсорный экран без физической клавиатуры (как на телефоне). */
function stubTouchOnly(matches: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: matches && query.includes("pointer: coarse"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}

/** Телефон в ландшафте: автоскрытие шапки включено, остальные медиа —
 * как в jsdom (не совпадают). */
function stubLandscape(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: query.includes("orientation: landscape"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}

/** Сенсорный телефон в ландшафте: совпадают и «нет мыши», и ориентация. */
function stubTouchLandscape(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches:
          query.includes("pointer: coarse") ||
          query.includes("orientation: landscape"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}

const frameInput = (host: HTMLElement): HTMLInputElement =>
  host.querySelector<HTMLInputElement>("input.frame-input")!;

beforeEach(() => {
  vi.stubGlobal("Image", FakeImage as unknown as typeof Image);
  vi.stubGlobal("requestAnimationFrame", () => {});
  pairDetail = oneFrame(50, 0);
  FakeImage.issued = [];
  vi.useFakeTimers({
    toFake: [
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
      "performance",
    ],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("первый кадр редактора", () => {
  it("до ответа — заглушка и кольцо, без «сломанной» картинки", async () => {
    const { host, unmount } = await mountEditor();
    expect(FakeImage.issued).toHaveLength(1);
    expect(frameIdx(FakeImage.issued[0].src)).toBe(0);
    const img = host.querySelector("img");
    expect(img?.getAttribute("src")).toBeFalsy();
    expect(host.querySelector(".frame-empty")).toBeTruthy();
    expect(host.querySelector(".frame-wait")).toBeTruthy();
    unmount();
  });

  it("кадр пришёл — он на экране, заглушка и кольцо ушли", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    const img = host.querySelector("img");
    expect(img?.getAttribute("src")).toContain("/frame/0/");
    expect(host.querySelector(".frame-empty")).toBeNull();
    expect(host.querySelector(".frame-wait")).toBeNull();
    unmount();
  });

  it("зависший ответ перевыпускается сам, без нажатий клавиш", async () => {
    const { host, unmount } = await mountEditor();
    const before = FakeImage.issued.length;
    act(() => {
      vi.advanceTimersByTime(16000);
    });
    expect(FakeImage.issued.length).toBeGreaterThan(before); // повтор случился
    await act(async () => {
      FakeImage.last().onload?.();
    });
    expect(host.querySelector("img")?.getAttribute("src"))
      .toContain("/frame/0/");
    unmount();
  });
});

/** Портретный вьюпорт телефона (jsdom по умолчанию 1024×768). */
function stubViewport(w: number, h: number): void {
  vi.stubGlobal("innerWidth", w);
  vi.stubGlobal("innerHeight", h);
}

describe("подсказка о повороте телефона", () => {
  it("16:9 на узком экране — поворот заметно крупнее, подсказка есть", async () => {
    pairDetail = { ...oneFrame(50, 0), width: 1920, height: 1080 };
    stubTouchOnly(true);
    stubViewport(360, 740);
    const rect = vi
      .spyOn(HTMLImageElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(0, 0, 360, 202.5));
    const { host, unmount } = await mountEditor();
    expect(host.querySelector(".rotate-hint")?.textContent).toContain(
      "Поверните телефон",
    );
    unmount();
    rect.mockRestore();
  });

  it("кадру 3:2 поворот почти ничего не даёт — подсказки нет", async () => {
    pairDetail = { ...oneFrame(50, 0), width: 1800, height: 1200 };
    stubTouchOnly(true);
    stubViewport(360, 740);
    const rect = vi
      .spyOn(HTMLImageElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(0, 0, 360, 240));
    const { host, unmount } = await mountEditor();
    expect(host.querySelector(".rotate-hint")).toBeNull();
    unmount();
    rect.mockRestore();
  });

  it("квадратному кадру не показывается", async () => {
    pairDetail = { ...oneFrame(50, 0), width: 32, height: 32 };
    stubTouchOnly(true);
    stubViewport(360, 740);
    const rect = vi
      .spyOn(HTMLImageElement.prototype, "getBoundingClientRect")
      .mockReturnValue(new DOMRect(0, 0, 360, 360));
    const { host, unmount } = await mountEditor();
    expect(host.querySelector(".rotate-hint")).toBeNull();
    unmount();
    rect.mockRestore();
  });
});

describe("ошибка загрузки кадра", () => {
  it("кольцо не прячется, попытки завершаются сообщением и паузой", async () => {
    const { host, unmount } = await mountEditor();
    // Попытка 1 падает — кольцо остаётся (раньше ошибка «показывала» кадр).
    act(() => {
      FakeImage.issued[0].onerror?.();
    });
    expect(host.querySelector(".frame-wait")).toBeTruthy();
    // 400 мс — эффект настроек применяет параметры (сброс ожиданий)…
    act(() => {
      vi.advanceTimersByTime(500);
    });
    // …далее тик pump делает повтор, он тоже падает.
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    act(() => {
      FakeImage.last().onerror?.();
    });
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    act(() => {
      FakeImage.last().onerror?.();
    });
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    act(() => {
      FakeImage.last().onerror?.();
    });
    expect(host.textContent).toContain("не загрузился");
    const stuck = FakeImage.issued.length;
    // Три неудачи — долбёжка прекращена.
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(FakeImage.issued.length).toBe(stuck);
    // Новое нав-действие — новая попытка.
    tap("ArrowRight");
    expect(frameIdx(FakeImage.last().src)).toBe(1);
    unmount();
  });
});

describe("навигация", () => {
  it("серия тапов — прыжок: промежуточные ответы не рисуются", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.(); // кадр 0 на экране
    });
    for (let i = 0; i < 3; i++) {
      tap("ArrowRight");
    }
    expect(FakeImage.targets().slice(1)).toEqual([1, 2, 3]);
    const img = () => host.querySelector("img");
    await act(async () => {
      FakeImage.issued[1].onload?.(); // кадр 1 пришёл первым
    });
    expect(img()?.getAttribute("src")).toContain("/frame/0/");
    await act(async () => {
      FakeImage.issued[3].onload?.(); // целевой кадр 3
    });
    expect(img()?.getAttribute("src")).toContain("/frame/3/");
    unmount();
  });

  it("удержание стрелки — стрим: догоняющие кадры показываются", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    tap("ArrowRight", true);
    tap("ArrowRight", true);
    const pending = FakeImage.targets().slice(1);
    expect(pending.length).toBeGreaterThanOrEqual(1);
    const target = pending[0];
    const arrived = FakeImage.issued.find(
      (im) => frameIdx(im.src) === target,
    );
    expect(arrived).toBeTruthy();
    await act(async () => {
      arrived!.onload?.();
    });
    expect(host.querySelector("img")?.getAttribute("src"))
      .toContain(`/frame/${target}/`);
    release("ArrowRight");
    unmount();
  });
});

describe("поле номера кадра", () => {
  it("показывает текущий кадр (1-based) и следует за навигацией", async () => {
    const { host, unmount } = await mountEditor();
    expect(frameInput(host)).toBeTruthy();
    expect(frameInput(host).value).toBe("1");
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    tap("ArrowRight");
    expect(frameInput(host).value).toBe("2");
    unmount();
  });

  it("ввод номера + Enter — переход ровно на целевой кадр", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    typeInto(frameInput(host), "7");
    expect(frameInput(host).value).toBe("7");
    keyPress(frameInput(host), "Enter");
    expect(frameIdx(FakeImage.last().src)).toBe(6);
    expect(frameInput(host).value).toBe("7"); // актуальный кадр, не черновик
    unmount();
  });

  it("ввод клампится в 1..N; пусто/мусор — откат", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    const input = () => frameInput(host);
    typeInto(input(), "999");
    keyPress(input(), "Enter");
    expect(frameIdx(FakeImage.last().src)).toBe(49); // последний из 50
    expect(input().value).toBe("50");
    typeInto(input(), "0");
    keyPress(input(), "Enter");
    // кадр 0 уже в кэше планировщика — новый запрос не выпускается,
    // показ мгновенный
    expect(host.querySelector("img")?.getAttribute("src")).toContain("/frame/0/");
    expect(input().value).toBe("1");
    typeInto(input(), "abc");
    expect(input().value).toBe(""); // не-цифры отброшены
    keyPress(input(), "Enter");
    expect(input().value).toBe("1"); // откат к текущему, без прыжка
    unmount();
  });

  it("ширина поля следует числу цифр (ввод длиннее total не обрезается)", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    const input = () => frameInput(host);
    // total=50 → 2 цифры + 1 запас
    expect(input().style.width).toBe("3ch");
    typeInto(input(), "9999");
    expect(input().style.width).toBe("5ch"); // подрастает под ввод
    keyPress(input(), "Enter");
    expect(input().style.width).toBe("3ch"); // клампинг → снова 2 цифры
    unmount();
  });

  it("Esc откатывает ввод к текущему кадру", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    typeInto(frameInput(host), "9");
    expect(frameInput(host).value).toBe("9");
    keyPress(frameInput(host), "Escape");
    expect(frameInput(host).value).toBe("1");
    unmount();
  });

  it("во время проигрывания поле обновляется вместе с позицией", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    tap("Space"); // начало воспроизведения
    act(() => {
      vi.advanceTimersByTime(1); // тик планировщика воспроизведения
    });
    expect(frameInput(host).value).toBe("2"); // позиция 1, кадр грузится
    unmount();
  });
});

describe("клавиатура не отбирает Tab", () => {
  it("Tab не перехватывается и не меняет пропорции кадра", async () => {
    const { host, unmount } = await mountEditor();
    const img = host.querySelector("img")!;
    expect(img.style.objectFit).toBe("contain");
    const e = new KeyboardEvent("keydown", {
      key: "Tab",
      code: "Tab",
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(e);
    });
    expect(e.defaultPrevented).toBe(false);
    expect(img.style.objectFit).toBe("contain");
    unmount();
  });

  it("A переключает «сохранять пропорции / растягивать»", async () => {
    const { host, unmount } = await mountEditor();
    const img = host.querySelector("img")!;
    tap("KeyA");
    expect(img.style.objectFit).toBe("fill");
    tap("KeyA");
    expect(img.style.objectFit).toBe("contain");
    unmount();
  });
});

function fragmentButton(host: HTMLElement, name: string): HTMLButtonElement {
  const bar = host.querySelector('[role="toolbar"][aria-label="Разметка фрагментов"]');
  if (!bar) throw new Error("панель разметки не найдена");
  // Кнопки панели — только значки: видимого текста у них нет, поэтому
  // сценарии ищут действие по доступному имени/подсказке.
  const btn = [...bar.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) =>
      (b.getAttribute("aria-label") ?? "").includes(name) ||
      (b.getAttribute("title") ?? "").includes(name),
  );
  if (!btn) throw new Error(`кнопка «${name}» не найдена`);
  return btn;
}

function clickFragment(host: HTMLElement, text: string): void {
  act(() => {
    fragmentButton(host, text).click();
  });
}

function commentButton(host: HTMLElement): HTMLButtonElement {
  const btn = host.querySelector<HTMLButtonElement>(
    ".editor-comment .comment-edit",
  );
  if (!btn) throw new Error("кнопка комментария не найдена");
  return btn;
}

/** Клик по плашке комментария и запуск отложенного фокуса textarea. */
function openComment(host: HTMLElement): void {
  act(() => commentButton(host).click());
  act(() => {
    vi.advanceTimersByTime(0);
  });
}

/** Переход к кадру через поле номера: клавиши в поле не считаются
 * физической клавиатурой, поэтому панель разметки остаётся на месте. */
function seekToFrame(host: HTMLElement, frame: number): void {
  const input = frameInput(host);
  act(() => input.focus());
  typeInto(input, String(frame));
  keyPress(input, "Enter");
}

function topButton(host: HTMLElement, text: string): HTMLButtonElement {
  const btn = [...host.querySelectorAll<HTMLButtonElement>(".editor-top button")].find(
    (b) => b.textContent?.includes(text),
  );
  if (!btn) throw new Error(`кнопка «${text}» в шапке не найдена`);
  return btn;
}

function menuTrigger(host: HTMLElement, name: string): HTMLButtonElement {
  const btn = [...host.querySelectorAll<HTMLButtonElement>("button[aria-expanded]")].find(
    (b) => b.textContent?.includes(name),
  );
  if (!btn) throw new Error(`меню «${name}» не найдено`);
  return btn;
}

function openMenu(host: HTMLElement, name: string): void {
  act(() => {
    menuTrigger(host, name).click();
  });
}

function menuPanel(name: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(
    `[role="dialog"][aria-label="${name}"]`,
  );
}

function playButton(host: HTMLElement): HTMLButtonElement {
  const btn = host.querySelector<HTMLButtonElement>("button.transport-play");
  if (!btn) throw new Error("кнопка воспроизведения не найдена");
  return btn;
}

function playbackSettings(host: HTMLElement): HTMLButtonElement {
  const btn = host.querySelector<HTMLButtonElement>(
    '.transport-playback button[aria-label="Направление и скорость"]',
  );
  if (!btn) throw new Error("кнопка настроек воспроизведения не найдена");
  return btn;
}

function directionButton(): HTMLButtonElement {
  const panel = menuPanel("Направление и скорость");
  if (!panel) throw new Error("меню воспроизведения не открыто");
  const btn = [...panel.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent?.includes("Вперёд") || b.textContent?.includes("Назад"),
  );
  if (!btn) throw new Error("переключатель направления не найден");
  return btn;
}

function lastSaved(): { start: number; end: number; comment: string }[] {
  const calls = vi.mocked(replaceFragments).mock.calls;
  return calls[calls.length - 1][1] as {
    start: number;
    end: number;
    comment: string;
  }[];
}

describe("панель разметки: сенсорный экран", () => {
  it("на устройстве с физической клавиатурой кнопок нет", async () => {
    const { host, unmount } = await mountEditor();
    expect(host.querySelector(".editor-workbench")).toBeNull();
    expect(
      host.querySelector('[role="toolbar"][aria-label="Разметка фрагментов"]'),
    ).toBeNull();
    unmount();
  });

  it("на сенсорном экране — только значки без подписей и клавиш", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    const bar = host.querySelector<HTMLElement>(
      '[role="toolbar"][aria-label="Разметка фрагментов"]',
    )!;
    expect(bar).toBeTruthy();
    const buttons = [...bar.querySelectorAll<HTMLButtonElement>("button")];
    expect(buttons).toHaveLength(7);
    for (const button of buttons) {
      expect(button.textContent?.trim()).toBe("");
      expect(button.querySelector("svg.fragment-icon")).toBeTruthy();
      expect(button.getAttribute("aria-label")).toBeTruthy();
    }
    expect(bar.querySelector("kbd")).toBeNull();
    unmount();
  });
});

describe("панель разметки", () => {
  beforeEach(() => {
    stubTouchOnly(true);
  });

  it("все действия фрагмента доступны кнопками", async () => {
    const { host, unmount } = await mountEditor();
    for (const name of [
      "Начало",
      "Конец",
      "Создать",
      "Удалить",
      "Комментарий",
      "Отменить",
      "Повторить",
    ]) {
      expect(fragmentButton(host, name)).toBeTruthy();
    }
    unmount();
  });

  it("у каждой кнопки разметки есть значок и доступное имя", async () => {
    const { host, unmount } = await mountEditor();
    const bar = host.querySelector<HTMLElement>(
      '[role="toolbar"][aria-label="Разметка фрагментов"]',
    )!;
    const buttons = [...bar.querySelectorAll<HTMLButtonElement>("button")];
    expect(buttons).toHaveLength(7);
    for (const button of buttons) {
      expect(button.querySelector("svg.fragment-icon")).toBeTruthy();
      expect(button.getAttribute("aria-label")).toBeTruthy();
    }
    unmount();
  });

  it("«Начало» и «Конец» создают фрагмент и сохраняют его", async () => {
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Начало");
    seekToFrame(host, 11);
    clickFragment(host, "Конец");
    expect(lastSaved()).toEqual([{ start: 0, end: 10, comment: "" }]);
    unmount();
  });

  it("«Создать» ставит начало, «Завершить» — конец", async () => {
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Создать");
    seekToFrame(host, 11);
    clickFragment(host, "Завершить");
    expect(lastSaved()).toEqual([{ start: 0, end: 10, comment: "" }]);
    unmount();
  });

  it("«Удалить» убирает фрагмент под курсором", async () => {
    pairDetail.fragments = [{ start: 0, end: 10, comment: "" }];
    const { host, unmount } = await mountEditor();
    seekToFrame(host, 6);
    clickFragment(host, "Удалить");
    expect(lastSaved()).toEqual([]);
    unmount();
  });

  it("«Комментарий» открывает поле ввода", async () => {
    pairDetail.fragments = [{ start: 0, end: 10, comment: "" }];
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Комментарий");
    expect(host.querySelector("textarea")).toBeTruthy();
    unmount();
  });

  it("«Отменить» возвращает предыдущее состояние", async () => {
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Начало");
    clickFragment(host, "Отменить");
    expect(lastSaved()).toEqual([]);
    unmount();
  });
});

describe("комментарий к сегменту", () => {
  beforeEach(() => {
    pairDetail.fragments = [{ start: 0, end: 10, comment: "Проверить знак" }];
  });

  it("показанный комментарий — кнопка с доступным именем", async () => {
    const { host, unmount } = await mountEditor();
    const btn = commentButton(host);
    expect(btn.type).toBe("button");
    expect(btn.textContent).toContain("Редактировать комментарий");
    expect(btn.textContent).toContain("Проверить знак");
    unmount();
  });

  it("клик по комментарию открывает поле с текущим текстом и фокусом", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area = host.querySelector<HTMLTextAreaElement>(
      ".editor-comment textarea",
    );
    expect(area?.value).toBe("Проверить знак");
    expect(document.activeElement).toBe(area);
    unmount();
  });

  it("на сенсорном экране в портрете клик тоже открывает поле", async () => {
    stubTouchOnly(true);
    stubViewport(390, 844);
    const { host, unmount } = await mountEditor();
    openComment(host);
    expect(host.querySelector(".editor-comment textarea")).toBeTruthy();
    unmount();
  });

  it("в ландшафте телефона клик тоже открывает поле", async () => {
    stubTouchLandscape();
    stubViewport(844, 390);
    const { host, unmount } = await mountEditor();
    openComment(host);
    expect(host.querySelector(".editor-comment textarea")).toBeTruthy();
    unmount();
  });

  it("«Сохранить» сохраняет новый текст и показывает статус", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Новый комментарий");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    await act(async () => {});
    expect(lastSaved()).toEqual([
      { start: 0, end: 10, comment: "Новый комментарий" },
    ]);
    expect(host.querySelector(".editor-comment textarea")).toBeNull();
    expect(host.querySelector(".save-state")?.textContent).toContain(
      "Сохранено",
    );
    unmount();
  });

  it("успешное сохранение показывает локальный статус без тоста", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Новый комментарий");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    await act(async () => {});
    expect(host.querySelector(".comment-saved")?.textContent).toContain(
      "Сохранено",
    );
    expect(host.querySelector(".flash")).toBeNull();
    expect(host.querySelector(".save-state")?.getAttribute("role")).toBeNull();
    unmount();
  });

  it("локальный статус исчезает и возвращает комментарий", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Новый комментарий");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    await act(async () => {});
    expect(host.querySelector(".comment-saved")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(host.querySelector(".comment-saved")).toBeNull();
    expect(
      host.querySelector(".editor-comment .comment-text")?.textContent,
    ).toContain("Новый комментарий");
    unmount();
  });

  it("ошибка сохранения комментария не показывает локальный успех", async () => {
    vi.mocked(replaceFragments).mockRejectedValueOnce(new Error("сеть"));
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Черновик");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    await act(async () => {});
    expect(host.querySelector(".comment-saved")).toBeNull();
    expect(host.querySelector(".flash")?.getAttribute("role")).toBe("alert");
    unmount();
  });

  it("«Отмена» закрывает поле без сохранения", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Не сохранять");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-cancel")!.click();
    });
    expect(host.querySelector(".editor-comment textarea")).toBeNull();
    expect(vi.mocked(replaceFragments)).not.toHaveBeenCalled();
    expect(
      host.querySelector(".editor-comment .comment-text")?.textContent,
    ).toBe("Проверить знак");
    unmount();
  });

  it("Escape закрывает поле без сохранения", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    act(() => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    expect(host.querySelector(".editor-comment textarea")).toBeNull();
    expect(vi.mocked(replaceFragments)).not.toHaveBeenCalled();
    unmount();
  });

  it("после «Сохранить» фокус возвращается на кнопку комментария", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(document.activeElement).toBe(commentButton(host));
    unmount();
  });

  it("после «Отмена» фокус возвращается на кнопку комментария", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-cancel")!.click();
    });
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(document.activeElement).toBe(commentButton(host));
    unmount();
  });

  it("после Escape фокус возвращается на кнопку комментария", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    act(() => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(document.activeElement).toBe(commentButton(host));
    unmount();
  });

  it("Enter оставляет новую строку, а не сохраняет", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Первая строка");
    act(() => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
      );
    });
    expect(host.querySelector(".editor-comment textarea")).toBe(area);
    expect(vi.mocked(replaceFragments)).not.toHaveBeenCalled();
    expect(host.querySelector(".comment-hint")?.textContent).toContain(
      "Ctrl+Enter",
    );
    unmount();
  });

  it("Ctrl+Enter сохраняет комментарий", async () => {
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Новая строка");
    act(() => {
      area.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
        }),
      );
    });
    await act(async () => {});
    expect(host.querySelector(".editor-comment textarea")).toBeNull();
    expect(lastSaved()).toEqual([
      { start: 0, end: 10, comment: "Новая строка" },
    ]);
    unmount();
  });

  it("пока сохранение идёт, статус это показывает", async () => {
    let finish: () => void = () => {};
    vi.mocked(replaceFragments).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = () => resolve([]);
        }),
    );
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Черновик");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    expect(host.querySelector(".save-state")?.textContent).toContain(
      "Сохраняем",
    );
    await act(async () => {
      finish();
    });
    expect(host.querySelector(".save-state")?.textContent).toContain(
      "Сохранено",
    );
    unmount();
  });

  it("ошибка сохранения остаётся в статусе", async () => {
    vi.mocked(replaceFragments).mockRejectedValueOnce(new Error("сеть"));
    const { host, unmount } = await mountEditor();
    openComment(host);
    const area =
      host.querySelector<HTMLTextAreaElement>(".editor-comment textarea")!;
    typeIntoArea(area, "Черновик");
    act(() => {
      host.querySelector<HTMLButtonElement>(".comment-save")!.click();
    });
    await act(async () => {});
    expect(host.querySelector(".save-state")?.textContent).toContain(
      "Ошибка сохранения",
    );
    unmount();
  });
});

describe("экспорт без фрагментов", () => {
  it("кнопка заблокирована и объясняет причину", async () => {
    const { host, unmount } = await mountEditor();
    const btn = topButton(host, "Экспорт");
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe("Нет фрагментов для экспорта");
    unmount();
  });

  it("E не запускает экспорт и объясняет причину", async () => {
    const { host, unmount } = await mountEditor();
    tap("KeyE");
    await act(async () => {});
    expect(vi.mocked(startExport)).not.toHaveBeenCalled();
    expect(host.querySelector(".flash")?.textContent).toContain(
      "Нет фрагментов для экспорта",
    );
    unmount();
  });

  it("после создания первого фрагмента кнопка включается", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Начало");
    const btn = topButton(host, "Экспорт");
    expect(btn.disabled).toBe(false);
    expect(btn.title).toBe("Экспорт (E)");
    unmount();
  });
});

describe("смысловые зоны верха редактора", () => {
  it("главные действия видны без раскрытия меню", async () => {
    const { host, unmount } = await mountEditor();
    expect(topButton(host, "Назад")).toBeTruthy();
    expect(topButton(host, "Экспорт")).toBeTruthy();
    expect(host.querySelector('button[aria-label="Справка"]')).toBeTruthy();
    expect(frameInput(host)).toBeTruthy();
    unmount();
  });

  it("«Вид» открывает масштаб, качество и пропорции", async () => {
    const { host, unmount } = await mountEditor();
    const trigger = menuTrigger(host, "Вид");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(menuPanel("Вид")).toBeNull();
    openMenu(host, "Вид");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const panel = menuPanel("Вид");
    expect(panel).toBeTruthy();
    expect(panel!.querySelector('input[aria-label="Масштаб кадров"]')).toBeTruthy();
    expect(panel!.querySelector('input[aria-label="Качество JPEG"]')).toBeTruthy();
    expect(panel!.textContent).toContain("Сохранять пропорции");
    unmount();
  });

  it("галочка пропорций переключает режим кадра", async () => {
    const { host, unmount } = await mountEditor();
    const img = host.querySelector("img")!;
    openMenu(host, "Вид");
    const checkbox = menuPanel("Вид")!.querySelector<HTMLInputElement>(
      'input[type="checkbox"]',
    )!;
    expect(checkbox.checked).toBe(true);
    act(() => {
      checkbox.click();
    });
    expect(img.style.objectFit).toBe("fill");
    unmount();
  });

  it("«Данные» открывает действия с разметкой и бэкапом", async () => {
    const { host, unmount } = await mountEditor();
    openMenu(host, "Данные");
    const panel = menuPanel("Данные");
    expect(panel).toBeTruthy();
    expect(panel!.textContent).toContain("Скачать разметку");
    expect(panel!.textContent).toContain("Импортировать разметку");
    expect(panel!.textContent).toContain("Скачать бэкап");
    unmount();
  });

  it("«Диагностика» монтирует кэш только при открытии", async () => {
    const { host, unmount } = await mountEditor();
    expect(getCache).not.toHaveBeenCalled();
    openMenu(host, "Диагностика");
    await act(async () => {});
    expect(getCache).toHaveBeenCalledTimes(1);
    expect(
      menuPanel("Диагностика")!.querySelector('button[aria-label^="GOP-кэш"]'),
    ).toBeTruthy();
    unmount();
  });

  it("Escape закрывает справку и не уводит из редактора", async () => {
    const onBack = vi.fn();
    const { host, unmount } = await mountEditor(onBack);
    act(() => {
      host.querySelector<HTMLButtonElement>('button[aria-label="Справка"]')!.click();
    });
    expect(host.querySelector(".help-panel")).toBeTruthy();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    expect(host.querySelector(".help-panel")).toBeNull();
    expect(onBack).not.toHaveBeenCalled();
    unmount();
  });

  it("Escape закрывает список экспорта и не уводит из редактора", async () => {
    pairDetail = {
      ...oneFrame(50, 0),
      fragments: [{ start: 0, end: 10, comment: "" }],
    };
    const onBack = vi.fn();
    const { host, unmount } = await mountEditor(onBack);
    await act(async () => {
      topButton(host, "Экспорт").click();
    });
    await act(async () => {});
    expect(host.querySelector(".export-list")).toBeTruthy();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    expect(host.querySelector(".export-list")).toBeNull();
    expect(onBack).not.toHaveBeenCalled();
    unmount();
  });

  it("Escape закрывает меню и не уводит из редактора", async () => {
    const onBack = vi.fn();
    const { host, unmount } = await mountEditor(onBack);
    const trigger = menuTrigger(host, "Вид");
    act(() => {
      trigger.focus();
      trigger.click();
    });
    expect(menuPanel("Вид")).toBeTruthy();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    expect(menuPanel("Вид")).toBeNull();
    expect(onBack).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(trigger);
    unmount();
  });

  it("при открытом меню горячие клавиши редактора не срабатывают", async () => {
    pairDetail.fragments = [{ start: 0, end: 10, comment: "" }];
    const { host, unmount } = await mountEditor();
    openMenu(host, "Вид");
    tap("Delete");
    expect(replaceFragments).not.toHaveBeenCalled();
    unmount();
  });

  it("предупреждение пары остаётся видимым в шапке", async () => {
    pairDetail.pair_warning = "источник и превью не совпадают";
    const { host, unmount } = await mountEditor();
    const warning = host.querySelector(".editor-warning");
    expect(warning?.getAttribute("title")).toContain("не совпадают");
    expect(host.textContent).toContain("не совпадают");
    unmount();
  });
});

describe("телефонная шапка: одно меню «Ещё»", () => {
  const setupTriggers = (host: HTMLElement) =>
    [...host.querySelectorAll<HTMLButtonElement>(".editor-top .popover-trigger")];

  it("на сенсорном экране вместо трёх меню — одно «Ещё»", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    const triggers = setupTriggers(host);
    expect(triggers).toHaveLength(1);
    expect(triggers[0].textContent).toContain("Ещё");
    unmount();
  });

  it("на устройстве с клавиатурой остаются три меню", async () => {
    const { host, unmount } = await mountEditor();
    const labels = setupTriggers(host).map((b) => b.textContent?.trim() ?? "");
    expect(labels.some((l) => l.includes("Вид"))).toBe(true);
    expect(labels.some((l) => l.includes("Данные"))).toBe(true);
    expect(labels.some((l) => l.includes("Диагностика"))).toBe(true);
    expect(labels.some((l) => l.includes("Ещё"))).toBe(false);
    unmount();
  });

  it("«Ещё» открывает настройки вида по умолчанию", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    openMenu(host, "Ещё");
    const panel = menuPanel("Ещё");
    expect(panel).toBeTruthy();
    expect(
      panel!.querySelector('input[aria-label="Масштаб кадров"]'),
    ).toBeTruthy();
    unmount();
  });

  it("вкладка «Данные» показывает разметку и бэкап", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    openMenu(host, "Ещё");
    const tab = [
      ...menuPanel("Ещё")!.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    ].find((b) => b.textContent === "Данные")!;
    act(() => {
      tab.click();
    });
    expect(menuPanel("Ещё")!.textContent).toContain("Скачать разметку");
    expect(menuPanel("Ещё")!.textContent).toContain("Скачать бэкап");
    unmount();
  });

  it("«Диагностика» внутри «Ещё» грузит кэш только при выборе", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    openMenu(host, "Ещё");
    expect(getCache).not.toHaveBeenCalled();
    const tab = [
      ...menuPanel("Ещё")!.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
    ].find((b) => b.textContent === "Диагностика")!;
    act(() => {
      tab.click();
    });
    await act(async () => {});
    expect(getCache).toHaveBeenCalledTimes(1);
    expect(
      menuPanel("Ещё")!.querySelector('button[aria-label^="GOP-кэш"]'),
    ).toBeTruthy();
    unmount();
  });

  it("Escape закрывает «Ещё» и возвращает фокус на кнопку", async () => {
    stubTouchOnly(true);
    const { host, unmount } = await mountEditor();
    const trigger = menuTrigger(host, "Ещё");
    act(() => {
      trigger.focus();
      trigger.click();
    });
    expect(menuPanel("Ещё")).toBeTruthy();
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    expect(menuPanel("Ещё")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    unmount();
  });
});

describe("управление воспроизведением", () => {
  it("кнопка скорости открывает направление и скорость", async () => {
    const { host, unmount } = await mountEditor();
    const trigger = playbackSettings(host);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(menuPanel("Направление и скорость")).toBeNull();
    act(() => {
      trigger.click();
    });
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const panel = menuPanel("Направление и скорость");
    expect(panel).toBeTruthy();
    expect(
      panel!.querySelector('input[aria-label="Скорость воспроизведения"]'),
    ).toBeTruthy();
    expect(panel!.textContent).toContain("Направление");
    expect(panel!.textContent).toContain("Вперёд");
    expect(panel!.querySelectorAll("button")).toHaveLength(1);
    unmount();
  });

  it("переключатель направления уводит воспроизведение назад", async () => {
    const { host, unmount } = await mountEditor();
    expect(playButton(host).getAttribute("aria-label")).toContain(
      "Воспроизвести вперёд",
    );
    act(() => {
      playbackSettings(host).click();
    });
    const toggle = directionButton();
    expect(toggle.textContent).toContain("Вперёд");
    act(() => {
      toggle.click();
    });
    expect(toggle.textContent).toContain("Назад");
    expect(playButton(host).getAttribute("aria-label")).toContain("назад");
    unmount();
  });

  it("повторное нажатие возвращает направление вперёд", async () => {
    const { host, unmount } = await mountEditor();
    act(() => {
      playbackSettings(host).click();
    });
    const toggle = directionButton();
    act(() => {
      toggle.click();
    });
    act(() => {
      toggle.click();
    });
    expect(toggle.textContent).toContain("Вперёд");
    expect(playButton(host).getAttribute("aria-label")).toContain("вперёд");
    unmount();
  });

  it("скорость из меню задаёт шаг воспроизведения", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    act(() => {
      playbackSettings(host).click();
    });
    const slider = menuPanel("Направление и скорость")!.querySelector<
      HTMLInputElement
    >('input[aria-label="Скорость воспроизведения"]')!;
    typeInto(slider, "3"); // 2^3 = x8
    expect(playbackSettings(host).textContent).toContain("x8");
    act(() => {
      playbackSettings(host).click(); // закрыть меню: иначе пробел подавлен
    });
    tap("Space");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(frameInput(host).value).toBe("9"); // 0 + 8 → кадр 9
    unmount();
  });

  it("клик по изображению запускает и ставит на паузу", async () => {
    const { host, unmount } = await mountEditor();
    await act(async () => {
      FakeImage.issued[0].onload?.();
    });
    const image = host.querySelector<HTMLImageElement>(".video-area img")!;
    act(() => {
      image.click();
    });
    expect(playButton(host).getAttribute("aria-pressed")).toBe("true");
    act(() => {
      image.click();
    });
    expect(playButton(host).getAttribute("aria-pressed")).toBe("false");
    unmount();
  });
});

describe("обратная связь о сохранении", () => {
  it("успех — role=status, исчезает сам", async () => {
    const { host, unmount } = await mountEditor();
    tap("ArrowUp");
    await act(async () => {});
    const flash = host.querySelector(".flash");
    expect(flash?.getAttribute("role")).toBe("status");
    expect(flash?.textContent).toContain("Сохранено");
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(host.querySelector(".flash")).toBeNull();
    unmount();
  });

  it("ошибка сохранения предлагает повторить", async () => {
    vi.mocked(replaceFragments).mockRejectedValueOnce(new Error("сеть"));
    const { host, unmount } = await mountEditor();
    tap("ArrowUp");
    await act(async () => {});
    const retry = host.querySelector<HTMLButtonElement>(".flash-retry");
    expect(retry?.textContent).toBe("Повторить");
    act(() => {
      retry!.click();
    });
    await act(async () => {});
    expect(host.querySelector(".flash")?.textContent).toContain("Сохранено");
    unmount();
  });

  it("ошибка — role=alert, не исчезает и закрывается вручную", async () => {
    vi.mocked(replaceFragments).mockRejectedValueOnce(new Error("сеть"));
    const { host, unmount } = await mountEditor();
    tap("ArrowUp");
    await act(async () => {});
    const flash = host.querySelector(".flash");
    expect(flash?.getAttribute("role")).toBe("alert");
    expect(flash?.className).toContain("error");
    expect(flash?.textContent).toContain("Ошибка сохранения");
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(host.querySelector(".flash")).not.toBeNull();
    act(() => {
      host.querySelector<HTMLButtonElement>(".flash-close")!.click();
    });
    expect(host.querySelector(".flash")).toBeNull();
    unmount();
  });
});

describe("кнопка справки", () => {
  it("названа словами, знак вопроса скрыт от скринридера", async () => {
    const { host, unmount } = await mountEditor();
    const help = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Справка"]',
    );
    expect(help).toBeTruthy();
    expect(help?.querySelector('[aria-hidden="true"]')?.textContent).toBe("?");
    unmount();
  });
});

function statusbar(host: HTMLElement): HTMLCanvasElement {
  const canvas = host.querySelector<HTMLCanvasElement>("canvas.statusbar");
  if (!canvas) throw new Error("таймлайн не найден");
  return canvas;
}

function keyOn(el: HTMLElement, key: string): KeyboardEvent {
  const e = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
  });
  act(() => {
    el.dispatchEvent(e);
  });
  return e;
}

describe("таймлайн редактора", () => {
  it("рядом свёрнутая легенда, объясняющая цвета", async () => {
    const { host, unmount } = await mountEditor();
    const legend = host.querySelector<HTMLDetailsElement>(".editor-legend");
    expect(legend).toBeTruthy();
    expect(legend?.open).toBe(false);
    expect(legend?.textContent).toContain("Легенда таймлайна");
    expect(legend?.textContent).toContain("фрагмент с комментарием");
    unmount();
  });

  it("фокусируемый слайдер: минимум, максимум и текущий кадр", async () => {
    const { host, unmount } = await mountEditor();
    const tl = statusbar(host);
    expect(tl.getAttribute("role")).toBe("slider");
    expect(tl.tabIndex).toBe(0);
    expect(tl.getAttribute("aria-valuemin")).toBe("1");
    expect(tl.getAttribute("aria-valuemax")).toBe("50");
    expect(tl.getAttribute("aria-valuenow")).toBe("1");
    expect(tl.getAttribute("aria-valuetext")).toBe("Кадр 1 из 50");
    unmount();
  });

  it("стрелки двигают на кадр, Home/End — к краям", async () => {
    const { host, unmount } = await mountEditor();
    const tl = statusbar(host);
    expect(keyOn(tl, "ArrowRight").defaultPrevented).toBe(true);
    expect(tl.getAttribute("aria-valuenow")).toBe("2");
    keyOn(tl, "End");
    expect(tl.getAttribute("aria-valuenow")).toBe("50");
    keyOn(tl, "Home");
    expect(tl.getAttribute("aria-valuenow")).toBe("1");
    keyOn(tl, "ArrowLeft");
    expect(tl.getAttribute("aria-valuenow")).toBe("1");
    unmount();
  });

  it("PageUp/PageDown прыгают по границам фрагментов", async () => {
    pairDetail.fragments = [
      { start: 0, end: 10, comment: "" },
      { start: 20, end: 30, comment: "" },
    ];
    const { host, unmount } = await mountEditor();
    const tl = statusbar(host);
    keyOn(tl, "PageDown");
    expect(tl.getAttribute("aria-valuenow")).toBe("11");
    keyOn(tl, "PageDown");
    expect(tl.getAttribute("aria-valuenow")).toBe("21");
    keyOn(tl, "PageUp");
    expect(tl.getAttribute("aria-valuenow")).toBe("11");
    unmount();
  });

  it("канва учитывает плотность пикселей (DPR)", async () => {
    vi.spyOn(window, "devicePixelRatio", "get").mockReturnValue(2);
    const { host, unmount } = await mountEditor();
    const tl = statusbar(host);
    expect(tl.width).toBe(1600);
    expect(tl.height).toBe(72);
    unmount();
  });
});

describe("полноэкранный режим", () => {
  it("видна кнопка выхода, пока редактор на весь экран", async () => {
    const { host, unmount } = await mountEditor();
    expect(host.querySelector(".fullscreen-exit")).toBeNull();
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => host.querySelector(".editor"),
    });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    const exit = host.querySelector<HTMLButtonElement>(".fullscreen-exit");
    expect(exit?.textContent).toContain("Выйти");
    unmount();
  });

  it("кнопка выхода компактна и названа словами", async () => {
    const { host, unmount } = await mountEditor();
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => host.querySelector(".editor"),
    });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    const exit = host.querySelector<HTMLButtonElement>(".fullscreen-exit")!;
    expect(exit.getAttribute("aria-label")).toBe(
      "Выйти из полноэкранного режима",
    );
    expect(exit.textContent).toContain("Выйти");
    unmount();
  });

  it("вход в полноэкранный режим фокусирует кнопку выхода", async () => {
    const { host, unmount } = await mountEditor();
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => host.querySelector(".editor"),
    });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(document.activeElement).toBe(
      host.querySelector(".fullscreen-exit"),
    );
    unmount();
  });

  it("выход из полноэкранного режима возвращает фокус в шапку", async () => {
    const { host, unmount } = await mountEditor();
    let full = true;
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => (full ? host.querySelector(".editor") : null),
    });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    full = false;
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    expect(document.activeElement).toBe(host.querySelector(".editor-back"));
    unmount();
  });

  it("кнопка выхода действительно выходит из полноэкранного режима", async () => {
    const exitFullscreen = vi.fn(() => Promise.resolve());
    Object.defineProperty(document, "exitFullscreen", {
      configurable: true,
      value: exitFullscreen,
    });
    const { host, unmount } = await mountEditor();
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => host.querySelector(".editor"),
    });
    act(() => {
      document.dispatchEvent(new Event("fullscreenchange"));
    });
    act(() => {
      host.querySelector<HTMLButtonElement>(".fullscreen-exit")!.click();
    });
    expect(exitFullscreen).toHaveBeenCalled();
    unmount();
  });

  it("без нативного API включает полный экран внутри приложения", async () => {
    // iPhone Safari не умеет Element.requestFullscreen: режим обязан
    // включиться своим классом, иначе кнопка «Во весь экран» мертва.
    const { host, unmount } = await mountEditor();
    tap("KeyF");
    const editor = host.querySelector(".editor")!;
    expect(editor.classList.contains("fullscreen")).toBe(true);
    expect(editor.classList.contains("fullscreen-fallback")).toBe(true);
    expect(host.querySelector(".fullscreen-exit")).toBeTruthy();
    unmount();
  });

  it("кнопка выхода выключает резервный полный экран", async () => {
    const { host, unmount } = await mountEditor();
    tap("KeyF");
    act(() => {
      host.querySelector<HTMLButtonElement>(".fullscreen-exit")!.click();
    });
    expect(host.querySelector(".editor")!.className).toBe("editor");
    unmount();
  });

  it("Esc выходит из резервного полного экрана, не закрывая редактор", async () => {
    const onBack = vi.fn();
    const { host, unmount } = await mountEditor(onBack);
    tap("KeyF");
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          code: "Escape",
          bubbles: true,
        }),
      );
    });
    expect(host.querySelector(".editor")!.classList.contains("fullscreen"))
      .toBe(false);
    expect(onBack).not.toHaveBeenCalled();
    unmount();
  });

  it("использует webkit-полноэкранный API, если стандартного нет", async () => {
    const webkitRequest = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "webkitRequestFullscreen", {
      configurable: true,
      value: webkitRequest,
    });
    try {
      const { host, unmount } = await mountEditor();
      tap("KeyF");
      expect(webkitRequest).toHaveBeenCalledTimes(1);
      Object.defineProperty(document, "webkitFullscreenElement", {
        configurable: true,
        get: () => host.querySelector(".editor"),
      });
      act(() => {
        document.dispatchEvent(new Event("webkitfullscreenchange"));
      });
      const editor = host.querySelector(".editor")!;
      expect(editor.classList.contains("fullscreen")).toBe(true);
      expect(editor.classList.contains("fullscreen-fallback")).toBe(false);
      unmount();
    } finally {
      delete (HTMLElement.prototype as { webkitRequestFullscreen?: unknown })
        .webkitRequestFullscreen;
      delete (document as { webkitFullscreenElement?: unknown })
        .webkitFullscreenElement;
    }
  });

  it("отказ нативного API включает резервный полный экран", async () => {
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: () => Promise.reject(new Error("нет разрешения")),
    });
    try {
      const { host, unmount } = await mountEditor();
      tap("KeyF");
      await act(async () => {
        await Promise.resolve();
      });
      const editor = host.querySelector(".editor")!;
      expect(editor.classList.contains("fullscreen")).toBe(true);
      expect(editor.classList.contains("fullscreen-fallback")).toBe(true);
      unmount();
    } finally {
      delete (HTMLElement.prototype as { requestFullscreen?: unknown })
        .requestFullscreen;
    }
  });
});

describe("ландшафтная шапка", () => {
  it("скрывается после бездействия", async () => {
    stubLandscape();
    const { host, unmount } = await mountEditor();
    const top = host.querySelector(".editor-top")!;
    expect(top.classList.contains("controls-hidden")).toBe(false);
    act(() => {
      vi.advanceTimersByTime(2600);
    });
    expect(top.classList.contains("controls-hidden")).toBe(true);
    unmount();
  });

  it("возвращается движением указателя", async () => {
    stubLandscape();
    const { host, unmount } = await mountEditor();
    act(() => {
      vi.advanceTimersByTime(2600);
    });
    expect(
      host.querySelector(".editor-top")!.classList.contains("controls-hidden"),
    ).toBe(true);
    act(() => {
      window.dispatchEvent(new Event("pointermove"));
    });
    expect(
      host.querySelector(".editor-top")!.classList.contains("controls-hidden"),
    ).toBe(false);
    unmount();
  });

  it("не прячется, пока фокус в шапке", async () => {
    stubLandscape();
    const { host, unmount } = await mountEditor();
    act(() => {
      host.querySelector<HTMLButtonElement>(".editor-back")!.focus();
      vi.advanceTimersByTime(2600);
    });
    expect(
      host.querySelector(".editor-top")!.classList.contains("controls-hidden"),
    ).toBe(false);
    unmount();
  });

  it("не прячется, пока открыто меню", async () => {
    stubLandscape();
    const { host, unmount } = await mountEditor();
    const view = [...host.querySelectorAll<HTMLButtonElement>(".popover-trigger")]
      .find((b) => b.textContent?.includes("Вид"))!;
    act(() => {
      view.click();
      vi.advanceTimersByTime(2600);
    });
    expect(
      host.querySelector(".editor-top")!.classList.contains("controls-hidden"),
    ).toBe(false);
    unmount();
  });
});

describe("телеметрия клиента", () => {
  it("скрыта, пока кадры не листаются", async () => {
    const { host, unmount } = await mountEditor();
    expect(host.textContent).not.toContain("к/с");
    unmount();
  });
});
