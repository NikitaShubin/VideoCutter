import { useEffect, useState } from "react";

const TOUCH_ONLY = "(hover: none) and (pointer: coarse)";

function touchOnly(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(TOUCH_ONLY).matches;
}

/**
 * Доступны ли горячие клавиши. На сенсорном экране без физической
 * клавиатуры — нет; первое же нажатие клавиши переключает справку
 * в клавиатурный режим (планшет с подключённой клавиатурой).
 */
export function useKeyboardMode(): boolean {
  const [keyboard, setKeyboard] = useState(() => !touchOnly());

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia(TOUCH_ONLY);
    const onChange = () => {
      if (!mq.matches) setKeyboard(true);
    };
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Unidentified") return;
      // Ввод в поле (в том числе с экранной клавиатуры телефона) — ещё не
      // признак физической клавиатуры: горячие клавиши в полях не работают.
      const target = event.target as HTMLElement | null;
      if (
        target?.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(target?.tagName ?? "")
      ) {
        return;
      }
      setKeyboard(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return keyboard;
}
