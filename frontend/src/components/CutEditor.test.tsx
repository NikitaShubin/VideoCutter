// Регрессии редактора на медленном сервере: экран не «залипает» навечно,
// ошибка кадра не крадёт кольцо ожидания, серия тапов — прыжок (без
// «анимации» промкадров), удержание стрелки — стрим с дропом.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CutEditor } from "./CutEditor";
import type { VideoPairDetail } from "../types";
import { replaceFragments, type CacheState } from "../api";

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
    getCache: () =>
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
    setPairSettings: () => Promise.resolve().then(() => undefined),
    replaceFragments: vi.fn(() => Promise.resolve()),
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

async function mountEditor() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<CutEditor pairId="ws-test" onBack={() => {}} />);
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

function fragmentButton(host: HTMLElement, text: string): HTMLButtonElement {
  const bar = host.querySelector('[role="toolbar"][aria-label="Разметка фрагментов"]');
  if (!bar) throw new Error("панель разметки не найдена");
  const btn = [...bar.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent?.includes(text),
  );
  if (!btn) throw new Error(`кнопка «${text}» не найдена`);
  return btn;
}

function clickFragment(host: HTMLElement, text: string): void {
  act(() => {
    fragmentButton(host, text).click();
  });
}

function lastSaved(): { start: number; end: number; comment: string }[] {
  const calls = vi.mocked(replaceFragments).mock.calls;
  return calls[calls.length - 1][1] as {
    start: number;
    end: number;
    comment: string;
  }[];
}

describe("видимая панель разметки", () => {
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

  it("«Начало» и «Конец» создают фрагмент и сохраняют его", async () => {
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Начало");
    for (let i = 0; i < 10; i++) tap("ArrowRight");
    clickFragment(host, "Конец");
    expect(lastSaved()).toEqual([{ start: 0, end: 10, comment: "" }]);
    unmount();
  });

  it("«Создать» ставит начало, «Завершить» — конец", async () => {
    const { host, unmount } = await mountEditor();
    clickFragment(host, "Создать");
    for (let i = 0; i < 10; i++) tap("ArrowRight");
    clickFragment(host, "Завершить");
    expect(lastSaved()).toEqual([{ start: 0, end: 10, comment: "" }]);
    unmount();
  });

  it("«Удалить» убирает фрагмент под курсором", async () => {
    pairDetail.fragments = [{ start: 0, end: 10, comment: "" }];
    const { host, unmount } = await mountEditor();
    for (let i = 0; i < 5; i++) tap("ArrowRight");
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
