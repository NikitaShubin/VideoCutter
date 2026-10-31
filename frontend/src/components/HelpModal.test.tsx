// Справка — настоящий модальный диалог: фокус входит в неё, Tab не
// выпускает наружу, Esc закрывает, а после закрытия фокус возвращается
// на кнопку «?». Тексты шорткатов должны совпадать с кодом.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HelpModal } from "./HelpModal";

function mount(node: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(node);
  });
  return {
    el: host,
    rerender: (next: React.ReactElement) => {
      act(() => root.render(next));
    },
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

function press(key: string, shiftKey = false): void {
  act(() => {
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key, shiftKey, bubbles: true }),
    );
  });
}

function stubTouchOnly(matches: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches:
          query === "(hover: none) and (pointer: coarse)" ? matches : false,
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("справка как диалог", () => {
  it("модальная и подписана заголовком", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const dialog = el.querySelector('[role="dialog"]');
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    expect(dialog?.getAttribute("aria-labelledby")).toBe("help-title");
    expect(el.querySelector("#help-title")?.textContent).toBe("Справка");
    unmount();
  });

  it("при открытии фокус на кнопке закрытия", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    expect(document.activeElement).toBe(el.querySelector(".help-close"));
    unmount();
  });

  it("Tab не выпускает фокус из диалога", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const dialog = el.querySelector('[role="dialog"]')!;
    const first = dialog.querySelector<HTMLElement>(".help-close")!;
    const links = dialog.querySelectorAll<HTMLAnchorElement>(".help-toc a");
    const last = links[links.length - 1];
    last.focus();
    press("Tab");
    expect(document.activeElement).toBe(first);
    press("Tab", true);
    expect(document.activeElement).toBe(last);
    unmount();
  });

  it("оглавление ведёт к существующим разделам", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const links = Array.from(
      el.querySelectorAll<HTMLAnchorElement>(".help-toc a"),
    );
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) {
      const id = link.getAttribute("href")!.slice(1);
      expect(el.querySelector(`#${id}`)).not.toBeNull();
    }
    unmount();
  });

  it("Esc закрывает справку", () => {
    const onClose = vi.fn();
    const { unmount } = mount(<HelpModal open onClose={onClose} />);
    press("Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("после закрытия возвращает фокус на кнопку-открывашку", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const { rerender, unmount } = mount(<HelpModal open onClose={() => {}} />);
    expect(document.activeElement).not.toBe(trigger);
    rerender(<HelpModal open={false} onClose={() => {}} />);
    expect(document.activeElement).toBe(trigger);
    unmount();
    trigger.remove();
  });

  it("видимый крестик не попадает в доступное имя", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const close = el.querySelector(".help-close");
    expect(close?.getAttribute("aria-label")).toBe("Закрыть справку");
    expect(close?.querySelector('[aria-hidden="true"]')?.textContent).toBe("✕");
    unmount();
  });

  it("шорткаты разметки описаны так же, как в коде", () => {
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const text = el.textContent ?? "";
    expect(text).toContain("↑ / [ или ] / ↓");
    expect(text).toContain("первое нажатие — начало, второе — конец");
    expect(text).toContain("отменить / повторить");
    expect(text).toContain("воспроизведение / пауза");
    expect(text).not.toContain("↑ / ] или [ / ↓");
    expect(text).not.toContain("undo / redo / redo");
    unmount();
  });
});

describe("справка под устройство", () => {
  it("на сенсорном экране показывает экранные кнопки", () => {
    stubTouchOnly(true);
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const text = el.textContent ?? "";
    expect(text).toContain("Сенсорный режим");
    expect(text).toContain("Начало фрагмента");
    expect(text).toContain("Комментарий");
    expect(
      el.querySelectorAll(".help-icon-table .fragment-icon"),
    ).toHaveLength(8);
    expect(text).not.toContain("Пробел");
    unmount();
  });

  it("на клавиатуре показывает горячие клавиши", () => {
    stubTouchOnly(false);
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    const text = el.textContent ?? "";
    expect(text).toContain("Пробел");
    expect(text).toContain("Клавиши");
    expect(text).not.toContain("Сенсорный режим");
    expect(el.querySelector(".help-icon-table")).toBeNull();
    unmount();
  });

  it("первое нажатие клавиши включает режим клавиатуры", () => {
    stubTouchOnly(true);
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    expect(el.textContent).toContain("Сенсорный режим");
    press("KeyR");
    expect(el.textContent).toContain("Пробел");
    expect(el.textContent).not.toContain("Сенсорный режим");
    unmount();
  });

  it("ввод в поле не включает режим клавиатуры", () => {
    stubTouchOnly(true);
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    const { el, unmount } = mount(<HelpModal open onClose={() => {}} />);
    act(() => {
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "1", bubbles: true }),
      );
    });
    expect(el.textContent).toContain("Сенсорный режим");
    unmount();
    input.remove();
  });
});
