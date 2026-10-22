import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

interface Props {
  /** Текст подсказки (показывается целиком). */
  text: string;
  /** Что видно на месте подсказки (обычно значок). */
  children: ReactNode;
  className?: string;
  /** Клик «залипает» подсказку (по умолчанию — да). */
  pin?: boolean;
  /** Спан фокусируется с клавиатуры (внутри кнопки — нет). */
  focusable?: boolean;
}

/** Подсказка по наведению И по клику.
 *
 * Нативный `title` не годится: по клику не срабатывает, появляется с
 * задержкой, а внутри строки списка обрезается `overflow: hidden` рамки.
 * Поэтому рисуем свою: слой поверх страницы (portal + `position: fixed`),
 * по наведению — мгновенно, по клику — «залипает» (закрыть — клик вне или
 * Escape).
 */
export function Hint({
  text,
  children,
  className,
  pin = true,
  focusable = true,
}: Props) {
  const anchor = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);

  const place = useCallback(() => {
    const el = anchor.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = Math.min(460, window.innerWidth - 24);
    const left = Math.max(12, Math.min(r.left, window.innerWidth - width - 12));
    const below = r.bottom + 8;
    const top = below + 220 < window.innerHeight ? below : Math.max(12, r.top - 8);
    setPos({ top, left });
    setOpen(true);
  }, []);

  const hide = useCallback(() => {
    if (pinned) return;
    setOpen(false);
  }, [pinned]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setPinned(false);
      setOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      if (!pinned) return;
      if (anchor.current?.contains(e.target as Node)) return;
      setPinned(false);
      setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, pinned]);

  return (
    <>
      <span
        ref={anchor}
        className={className}
        tabIndex={focusable ? 0 : undefined}
        aria-label={focusable ? text : undefined}
        onMouseEnter={place}
        onMouseLeave={hide}
        onFocus={place}
        onBlur={() => {
          if (!pinned) setOpen(false);
        }}
        onClick={
          pin
            ? (e) => {
                e.stopPropagation();
                setPinned((p) => !p);
                place();
              }
            : undefined
        }
      >
        {children}
      </span>
      {open &&
        pos &&
        typeof document !== "undefined" &&
        createPortal(
          <div className="hint-pop" role="tooltip" style={{ top: pos.top, left: pos.left }}>
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}
