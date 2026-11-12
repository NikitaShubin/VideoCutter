// Регрессия: сломанная задача должна рисоваться тем же каркасом, что и
// обычная строка (рамка .pair-open + подписи + кнопки). Раньше битая строка
// версталась отдельно — рамки не было, подписи теряли форматирование, а
// кнопка «изменить» исчезала.
//
// Договорённость с пользователем: поломка показывается «мягко-болезненно»
// (без растягивания рамки текстом ошибки), треугольник ⚠ несёт полный
// текст ошибки в подсказке (своей, не нативным title), а имя сломанного
// файла подсвечивается прямо в строке.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listProjects } from "../api";
import { PairList } from "./PairList";
import { RoleField } from "./RoleFields";
import type { VideoPair } from "../types";

// Список сам ходит в сервис за реестром проектов — в тестах он отвечает
// заглушкой, остальные вызовы API остаются настоящими.
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listProjects: vi.fn(async () => []) };
});

function makePair(overrides: Partial<VideoPair> = {}): VideoPair {
  return {
    id: "cam-2",
    name: "",
    source_name: "source.mp4",
    preview_name: "preview.mp4",
    unassigned_name: null,
    total_frames: 0,
    width: 0,
    height: 0,
    fps: 0,
    source_frames: 0,
    preview_frames: 0,
    source_skipped: 0,
    preview_skipped: 0,
    visible_match: false,
    pair_warning: "",
    fragments: [],
    position: 0,
    updated_at: 0,
    video_ver: "",
    quality: 78,
    scale: 0.75,
    indexing: false,
    indexing_progress: null,
    broken: true,
    error: "Ошибка открытия видеофайла: /data/workspaces/cam-2/preview.mp4",
    error_role: null,
    created_at: null,
    last_opened_at: null,
    project_id: null,
    export: null,
    ...overrides,
  };
}

function renderList(pairs: VideoPair[]): string {
  return renderToStaticMarkup(
    <PairList
      pairs={pairs}
      onSelect={() => {}}
      onChanged={() => {}}
      onTick={() => {}}
      onReload={async () => pairs}
      uploads={[]}
      onStartUpload={() => {}}
      onCancelUpload={() => {}}
      onDismissUpload={() => {}}
      onRenameUpload={() => {}}
      onUpdateJob={() => {}}
      onRemoveJob={() => {}}
      onLogout={null}
    />,
  );
}

/** HTML без подсказок (title/aria-label) — что видит пользователь в строке. */
function visibleText(html: string): string {
  return html.replace(/title="[^"]*"/g, "").replace(/aria-label="[^"]*"/g, "");
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

/** Список в живом DOM (нужны клики: панель задачи открывается по карандашу). */
function mountList(pairs: VideoPair[], onReload?: () => Promise<VideoPair[]>) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(
      <PairList
        pairs={pairs}
        onSelect={() => {}}
        onChanged={() => {}}
        onTick={() => {}}
        onReload={onReload ?? (async () => pairs)}
        uploads={[]}
        onStartUpload={() => {}}
        onCancelUpload={() => {}}
        onDismissUpload={() => {}}
        onRenameUpload={() => {}}
        onUpdateJob={() => {}}
        onRemoveJob={() => {}}
        onLogout={null}
      />,
    );
  });
  return {
    host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

/** Кнопка «Изменить» (карандаш) первой строки. */
function pencil(): HTMLButtonElement {
  const btn = document.querySelector<HTMLButtonElement>(
    '.pair-edit-btn[title="Изменить"]',
  );
  if (!btn) throw new Error("кнопка «Изменить» не найдена");
  return btn;
}

const BROKEN = makePair();

beforeEach(() => {
  // Тесты делят один jsdom: вкладка/фильтры, записанные предыдущим
  // сценарием, иначе утекают в следующий и прячут строки задач.
  window.localStorage.clear();
});

describe("сломанная задача в списке", () => {
  it("рисуется в общем каркасе (есть рамка .pair-open)", () => {
    expect(renderList([BROKEN])).toContain('class="pair-open"');
  });

  it("подпись задачи внутри каркаса", () => {
    const html = renderList([BROKEN]);
    const frame = html.indexOf('class="pair-open"');
    const label = html.indexOf('class="pair-label"');
    expect(frame).toBeGreaterThan(-1);
    expect(label).toBeGreaterThan(frame);
  });

  it("есть кнопка изменения", () => {
    expect(renderList([BROKEN])).toContain('title="Изменить"');
  });

  it("есть кнопка удаления", () => {
    expect(renderList([BROKEN])).toContain('title="Удалить workspace"');
  });

  it("текст ошибки не растягивает строку (только в подсказке)", () => {
    const html = renderList([BROKEN]);
    expect(visibleText(html)).not.toContain(BROKEN.error);
  });

  it("полный текст ошибки доступен как подсказка треугольника", () => {
    const html = renderList([BROKEN]);
    expect(html).toContain('class="pair-bad-mark"');
    expect(html).toContain(BROKEN.error);
  });

  it("имя сломанного файла подсвечено в строке", () => {
    const html = renderList([makePair({ error_role: "preview" })]);
    expect(html).toContain('class="pair-role-bad"');
    expect(html).toContain("preview.mp4");
  });

  it("целый файл не подсвечен", () => {
    const html = renderList([makePair({ error_role: "preview" })]);
    const bad = html.slice(html.indexOf('class="pair-role-bad"'));
    expect(bad.indexOf("source.mp4")).toBe(-1);
  });
});

describe("панель роли (✎)", () => {
  it("битая роль подсвечена", () => {
    const html = renderToStaticMarkup(
      <RoleField
        role="preview"
        name="preview.mp4"
        shared={false}
        broken
        hint="⚠ не читается"
      />,
    );
    expect(html).toContain("role-field role-bad");
    expect(html).toContain("не читается");
  });

  it("имя битого файла показано вместо «(то же видео)»", () => {
    const html = renderToStaticMarkup(
      <RoleField
        role="source"
        name="source.mp4"
        shared
        broken
        hint="⚠ не читается"
      />,
    );
    expect(html).toContain("source.mp4");
    expect(html).not.toContain("(то же видео)");
  });

  it("целая роль не подсвечена", () => {
    const html = renderToStaticMarkup(
      <RoleField
        role="preview"
        name="preview.mp4"
        shared={false}
        broken={false}
        hint=""
      />,
    );
    expect(html).toContain('class="role-field"');
    expect(html).not.toContain("role-bad");
  });
});

describe("импорт/экспорт в панели задачи", () => {
  it("экспорт — стрелка вниз, импорт — стрелка вверх", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    act(() => {
      pencil().click();
    });
    const html = host.innerHTML;
    expect(html).toContain("⤓ Бэкап");
    expect(html).toContain("⤓ Разметка");
    expect(html).toContain("⤒ Импорт");
    unmount();
  });

  it("подсказка по наведению говорит, где экспорт, а где импорт", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    act(() => {
      pencil().click();
    });
    const chip = [...host.querySelectorAll(".role-action")].find((el) =>
      el.textContent?.includes("⤓ Бэкап"),
    );
    expect(chip).toBeTruthy();
    act(() => {
      chip!.dispatchEvent(
        new MouseEvent("mouseover", { bubbles: true, relatedTarget: null }),
      );
    });
    expect(document.querySelector(".hint-pop")?.textContent).toContain(
      "Экспорт бэкапа",
    );
    unmount();
  });

  it("у импорта подсказка про импорт", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    act(() => {
      pencil().click();
    });
    const chip = [...host.querySelectorAll(".role-action")].find((el) =>
      el.textContent?.includes("⤒ Импорт"),
    );
    expect(chip).toBeTruthy();
    act(() => {
      chip!.dispatchEvent(
        new MouseEvent("mouseover", { bubbles: true, relatedTarget: null }),
      );
    });
    expect(document.querySelector(".hint-pop")?.textContent).toContain(
      "Импорт разметки",
    );
    unmount();
  });
});

describe("карандаш открывает и закрывает панель задачи", () => {
  it("повторное нажатие карандаша закрывает панель", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    act(() => {
      pencil().click();
    });
    expect(host.querySelector(".pair-edit")).not.toBeNull();
    act(() => {
      pencil().click();
    });
    expect(host.querySelector(".pair-edit")).toBeNull();
    unmount();
  });

  it("панель другой задачи открывается независимо", () => {
    const { host, unmount } = mountList([
      makePair({ broken: false, id: "a" }),
      makePair({ broken: false, id: "b" }),
    ]);
    const pencils = document.querySelectorAll<HTMLButtonElement>(
      '.pair-edit-btn[title="Изменить"]',
    );
    act(() => {
      pencils[0].click();
    });
    expect(host.querySelectorAll(".pair-edit")).toHaveLength(1);
    act(() => {
      pencils[1].click();
    });
    expect(host.querySelectorAll(".pair-edit")).toHaveLength(1);
    unmount();
  });
});

describe("кнопки-иконки названы словами", () => {
  it("карандаш и корзина строки имеют доступные имена, значки скрыты", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const edit = host.querySelector<HTMLButtonElement>(
      '.pair-edit-btn[title="Изменить"]',
    )!;
    expect(edit.getAttribute("aria-label")).toBe("Изменить");
    expect(edit.querySelector('[aria-hidden="true"]')?.textContent).toBe("✎");
    const del = host.querySelector<HTMLButtonElement>(
      '.pair-delete[title="Удалить workspace"]',
    )!;
    expect(del.getAttribute("aria-label")).toBe("Удалить workspace");
    expect(del.querySelector('[aria-hidden="true"]')?.textContent).toBe("🗑");
    unmount();
  });

  it("восстановление из бэкапа — настоящая кнопка, доступная с клавиатуры", () => {
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const projectsTab = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find((b) => b.textContent?.includes("Проекты"))!;
    act(() => {
      projectsTab.click();
    });
    const restore = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
      (b) => b.textContent?.includes("Восстановить из бэкапа"),
    );
    expect(restore).toBeTruthy();
    expect(restore?.type).toBe("button");
    unmount();
  });
});

describe("действия строки в мобильном меню", () => {
  it("у строки есть одно меню действий", () => {
    const { host, unmount } = mountList([
      makePair({ broken: false, name: "Длинная задача" }),
    ]);
    const more = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Действия: Длинная задача"]',
    );
    expect(more).toBeTruthy();
    expect(more?.textContent).toContain("⋯");
    unmount();
  });

  it("меню открывает изменение и удаление", () => {
    const { host, unmount } = mountList([
      makePair({ broken: false, name: "Длинная задача" }),
    ]);
    const more = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Действия: Длинная задача"]',
    )!;
    act(() => {
      more.click();
    });
    const panel = document.querySelector(".popover-panel");
    expect(panel?.textContent).toContain("Изменить");
    expect(panel?.textContent).toContain("Удалить");
    unmount();
  });

  it("«Изменить» из меню открывает панель задачи", () => {
    const { host, unmount } = mountList([
      makePair({ broken: false, name: "Длинная задача" }),
    ]);
    const more = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Действия: Длинная задача"]',
    )!;
    act(() => {
      more.click();
    });
    const edit = [...document.querySelectorAll<HTMLButtonElement>(".popover-panel button")]
      .find((b) => b.textContent?.includes("Изменить"))!;
    act(() => {
      edit.click();
    });
    expect(host.querySelector(".pair-edit")).toBeTruthy();
    expect(document.querySelector(".popover-panel")).toBeNull();
    unmount();
  });
});

describe("превью таймлайна в списке", () => {
  it("декоративно и не попадает в дерево доступности", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const html = renderList([makePair({ broken: false })]);
    expect(html).toContain('class="pair-bg"');
    expect(html).toContain('aria-hidden="true"');
  });

  it("легенда — видимой плашкой под списком, а не в подсказке полосы", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const html = renderList([makePair({ broken: false })]);
    expect(html).toContain('class="timeline-legend"');
    for (const meaning of [
      "зелёный — свободно",
      "красный — фрагмент",
      "фрагмент с комментарием",
      "после текущего кадра",
      "выделено поверх свободного",
      "выделено поверх фрагмента",
      "текущий кадр",
      "отправка файла",
      "приём сервером",
      "индексация",
    ]) {
      expect(html).toContain(meaning);
    }
    expect(html).not.toContain("Цвета:");
    const strip = /<canvas[^>]*class="pair-bg"[^>]*>/.exec(html)?.[0] ?? "";
    expect(strip).not.toContain("title=");
  });

  it("плашка легенды стоит после списка задач", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const html = renderList([makePair({ broken: false })]);
    expect(html.indexOf("</ul>")).toBeLessThan(
      html.indexOf("timeline-legend"),
    );
  });

  it("у индексируемой задачи полоса не обещает легенду разметки", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const html = renderList([
      makePair({ broken: false, indexing: true, indexing_progress: 0.4 }),
    ]);
    const host = document.createElement("div");
    host.innerHTML = html;
    expect(host.querySelector(".pair-bg")?.hasAttribute("title")).toBe(false);
  });
});

describe("синхронизация с диском", () => {
  function syncButton(host: HTMLElement): HTMLButtonElement {
    const button = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Синхронизировать с диском"]',
    );
    if (!button) throw new Error("кнопка синхронизации не найдена");
    return button;
  }

  it("кнопка перечитывает задачи и проекты", async () => {
    const onReload = vi.fn(async () => [makePair()]);
    const { host, unmount } = mountList([makePair()], onReload);
    await act(async () => {
      syncButton(host).click();
    });
    expect(onReload).toHaveBeenCalled();
    expect(vi.mocked(listProjects)).toHaveBeenCalled();
    unmount();
  });

  it("сообщает о расхождениях, найденных на диске", async () => {
    const cuts = { state: "done", index: 1, total: 1, error: "" };
    const before = [makePair({ export: cuts })];
    const after = [makePair({ export: null })];
    const { host, unmount } = mountList(before, async () => after);
    await act(async () => {
      syncButton(host).click();
    });
    expect(host.textContent).toContain("пропали нарезки");
    unmount();
  });

  it("без расхождений сообщает, что всё в порядке", async () => {
    const pairs = [makePair()];
    const { host, unmount } = mountList(pairs, async () => pairs);
    await act(async () => {
      syncButton(host).click();
    });
    expect(host.textContent).toContain("Расхождений не найдено");
    unmount();
  });

  it("перечитка по фокусу вкладки молчит: сообщений нет", async () => {
    const onReload = vi.fn(async () => [makePair()]);
    const { host, unmount } = mountList([makePair()], onReload);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(onReload).toHaveBeenCalled();
    expect(host.textContent).not.toContain("Расхождений не найдено");
    expect(host.textContent).not.toContain("пропали нарезки");
    unmount();
  });
});

describe("семантика страницы", () => {
  it("у экрана есть заголовок первого уровня", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const html = renderList([makePair({ broken: false })]);
    expect(html).toContain("<h1");
    expect(html).toContain("VideoCutter");
  });

  it("вкладки переключаются стрелками, фокус следует за выбором", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const tasks = host.querySelector<HTMLButtonElement>("#tab-tasks")!;
    tasks.focus();
    act(() => {
      tasks.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "ArrowRight",
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    const projects = host.querySelector<HTMLButtonElement>("#tab-projects")!;
    expect(projects.getAttribute("aria-selected")).toBe("true");
    expect(tasks.getAttribute("aria-selected")).toBe("false");
    expect(document.activeElement).toBe(projects);
    unmount();
  });
});

describe("фильтры списка", () => {
  it("свёрнуты по умолчанию и раскрываются кнопкой", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const toggle = host.querySelector<HTMLButtonElement>(".filters-toggle")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(
      host.querySelector(".filters-fields")!.classList.contains("open"),
    ).toBe(false);
    act(() => {
      toggle.click();
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(
      host.querySelector(".filters-fields")!.classList.contains("open"),
    ).toBe(true);
    unmount();
  });

  it("активные фильтры видны на свёрнутой кнопке", () => {
    window.localStorage.setItem("vc-project", "__none__");
    window.localStorage.setItem("vc-sort", "name");
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const toggle = host.querySelector<HTMLButtonElement>(".filters-toggle")!;
    expect(toggle.getAttribute("aria-label")).toContain("активны");
    expect(toggle.querySelector(".filters-badge")?.textContent).toBe("2");
    unmount();
  });

  it("сброс фильтров возвращает задачи и убирает индикатор", () => {
    window.localStorage.setItem("vc-project", "__none__");
    window.localStorage.setItem("vc-sort", "name");
    const { host, unmount } = mountList([
      makePair({ broken: false, project_id: "p1" }),
    ]);
    expect(host.querySelector(".pair-open")).toBeNull();
    const toggle = host.querySelector<HTMLButtonElement>(".filters-toggle")!;
    act(() => {
      toggle.click();
    });
    const reset = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
      (b) => b.textContent?.includes("Сбросить фильтры"),
    )!;
    act(() => {
      reset.click();
    });
    expect(host.querySelector(".pair-open")).toBeTruthy();
    expect(toggle.getAttribute("aria-label")).toBe("Фильтры");
    expect(toggle.querySelector(".filters-badge")).toBeNull();
    expect(host.querySelectorAll<HTMLSelectElement>("#pair-filters select")[0].value).toBe("updated");
    expect(host.querySelectorAll<HTMLSelectElement>("#pair-filters select")[1].value).toBe("__all__");
    unmount();
  });

  it("пустой поиск предлагает сброс и возвращает список", () => {
    window.localStorage.setItem("vc-tab", "tasks");
    const { host, unmount } = mountList([makePair({ broken: false })]);
    const search = host.querySelector<HTMLInputElement>(".search")!;
    typeInto(search, "zzzz");
    expect(host.textContent).toContain("По запросу «zzzz» ничего не найдено.");
    const reset = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
      (b) => b.textContent?.includes("Сбросить поиск"),
    );
    expect(reset).toBeTruthy();
    act(() => {
      reset!.click();
    });
    expect(search.value).toBe("");
    expect(host.querySelector(".pair-open")).toBeTruthy();
    unmount();
  });
});
