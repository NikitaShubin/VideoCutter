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
import { describe, expect, it } from "vitest";
import { PairList } from "./PairList";
import { RoleField } from "./RoleFields";
import type { VideoPair } from "../types";

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

/** Список в живом DOM (нужны клики: панель задачи открывается по карандашу). */
function mountList(pairs: VideoPair[]) {
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
