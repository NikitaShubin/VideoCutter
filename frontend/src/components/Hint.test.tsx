// Подсказка о поломке: нативный title не годится (не срабатывает по
// клику, обрезается рамкой строки) — рисуем свою: по наведению мгновенно,
// по клику «залипает».
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { Hint } from "./Hint";

function mount(node: React.ReactElement): { el: HTMLElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(node);
  });
  return {
    el: host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

function hover(el: Element) {
  act(() => {
    el.dispatchEvent(
      new MouseEvent("mouseover", { bubbles: true, relatedTarget: null }),
    );
  });
}

function click(el: Element) {
  act(() => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

describe("подсказка", () => {
  it("по наведению показывает текст", () => {
    const { el, unmount } = mount(<Hint text="Ошибка открытия файла">⚠</Hint>);
    expect(document.querySelector(".hint-pop")).toBeNull();
    hover(el.querySelector("span")!);
    expect(document.querySelector(".hint-pop")?.textContent).toBe(
      "Ошибка открытия файла",
    );
    unmount();
  });

  it("по клику показывает текст (и держит его)", () => {
    const { el, unmount } = mount(<Hint text="Ошибка открытия файла">⚠</Hint>);
    click(el.querySelector("span")!);
    const pop = document.querySelector(".hint-pop");
    expect(pop?.textContent).toBe("Ошибка открытия файла");
    // Уход курсора не убирает «залипшую» подсказку.
    act(() => {
      el.querySelector("span")!.dispatchEvent(
        new MouseEvent("mouseout", { bubbles: true, relatedTarget: null }),
      );
    });
    expect(document.querySelector(".hint-pop")).not.toBeNull();
    unmount();
  });

  it("текст ошибки доступен и без наведения (aria-label)", () => {
    const { el, unmount } = mount(<Hint text="Ошибка открытия файла">⚠</Hint>);
    expect(el.querySelector("span")?.getAttribute("aria-label")).toBe(
      "Ошибка открытия файла",
    );
    unmount();
  });
});
