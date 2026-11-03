import { useEffect, useState } from "react";

const TOUCH_ONLY = "(hover: none) and (pointer: coarse)";

function isTouchOnly(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(TOUCH_ONLY).matches;
}

/**
 * Доступны ли горячие клавиши. На сенсорном экране без физической
 * клавиатуры — нет; первое же нажатие клавиши переключает справку
 * в клавиатурный режим (планшет с подключённой клавиатурой).
 *
 * Режим следует за текущим устройством: если экран стал сенсорным
 * (эмуляция в DevTools, отключённая мышь), подсказки снова прячутся.
 * Иначе флаг, вычисленный при загрузке, залипал бы на весь сеанс.
 */
export function useKeyboardMode(): boolean {
  const [touchOnly, setTouchOnly] = useState(isTouchOnly);
  const [keySeen, setKeySeen] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia(TOUCH_ONLY);
    const onChange = () => {
      setTouchOnly(mq.matches);
      // Переход в сенсорный режим — явный признак, что клавиатура больше
      // не основной ввод; следы прошлых нажатий не должны оставлять
      // подсказки на экране.
      if (mq.matches) setKeySeen(false);
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
      setKeySeen(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return !touchOnly || keySeen;
}
