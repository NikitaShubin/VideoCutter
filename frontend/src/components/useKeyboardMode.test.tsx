import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useKeyboardMode } from "./useKeyboardMode";

function Probe() {
  const keyboard = useKeyboardMode();
  return <span>{keyboard ? "keyboard" : "touch"}</span>;
}

/** matchMedia с управляемым ответом на «сенсорный экран без мыши». */
function stubTouchMedia(initialTouch: boolean) {
  let touch = initialTouch;
  const listeners = new Set<() => void>();

  const media = (isTouchQuery: boolean): MediaQueryList =>
    ({
      get matches() {
        return isTouchQuery ? touch : false;
      },
      media: "",
      onchange: null,
      addEventListener: (_type: string, listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: () => void) => {
        listeners.delete(listener);
      },
      addListener: (listener: () => void) => listeners.add(listener),
      removeListener: (listener: () => void) => listeners.delete(listener),
      dispatchEvent: () => true,
    }) as unknown as MediaQueryList;

  vi.stubGlobal("matchMedia", (query: string) =>
    media(query.includes("pointer: coarse")),
  );

  return (next: boolean) => {
    touch = next;
    act(() => listeners.forEach((listener) => listener()));
  };
}

function mountProbe(): { host: HTMLElement; unmount: () => void } {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<Probe />);
  });
  return {
    host,
    unmount: () => {
      act(() => root.unmount());
      host.remove();
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("режим клавиатуры", () => {
  it("переключение на сенсорный экран выключает клавиатурные подсказки", () => {
    const setTouch = stubTouchMedia(false);
    const { host, unmount } = mountProbe();
    expect(host.textContent).toBe("keyboard");

    setTouch(true);

    expect(host.textContent).toBe("touch");
    unmount();
  });

  it("нажатие физической клавиши возвращает клавиатурный режим", () => {
    stubTouchMedia(true);
    const { host, unmount } = mountProbe();
    expect(host.textContent).toBe("touch");

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
    });

    expect(host.textContent).toBe("keyboard");
    unmount();
  });
});
