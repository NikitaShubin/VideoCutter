// Файловый выбор — настоящая кнопка, а не label со скрытым input:
// keyboard-only пользователь должен дойти до неё Tab'ом и нажать Enter.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { FileButton } from "./FileButton";

function mount(node: React.ReactElement) {
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

describe("файловая кнопка", () => {
  it("это button, а скрытый input живёт рядом", () => {
    const { el, unmount } = mount(
      <FileButton onFile={() => {}}>Загрузить</FileButton>,
    );
    const button = el.querySelector("button");
    expect(button?.textContent).toBe("Загрузить");
    expect(button?.type).toBe("button");
    const input = el.querySelector('input[type="file"]');
    expect(input?.hasAttribute("hidden")).toBe(true);
    unmount();
  });

  it("клик по кнопке открывает системный выбор файла", () => {
    const click = vi.spyOn(HTMLInputElement.prototype, "click");
    const { el, unmount } = mount(
      <FileButton onFile={() => {}}>Загрузить</FileButton>,
    );
    act(() => {
      el.querySelector<HTMLButtonElement>("button")!.click();
    });
    expect(click).toHaveBeenCalledTimes(1);
    click.mockRestore();
    unmount();
  });

  it("выбранный файл отдаётся наверх, input сбрасывается", () => {
    const onFile = vi.fn();
    const { el, unmount } = mount(
      <FileButton onFile={onFile}>Загрузить</FileButton>,
    );
    const input = el.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = new File(["x"], "backup.zip", { type: "application/zip" });
    Object.defineProperty(input, "files", {
      value: [file],
      configurable: true,
    });
    act(() => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onFile).toHaveBeenCalledWith(file);
    expect(input.value).toBe("");
    unmount();
  });
});
