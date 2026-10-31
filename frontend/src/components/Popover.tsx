import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

interface Props {
  /** Имя меню: подпись кнопки и доступное имя панели. */
  label: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}

/** Немодальное меню-поповер с якорем у кнопки.
 *
 * Панель рендерится в portal и позиционируется fixed: её не режут
 * `overflow` родителей и она не уезжает за край на узком экране.
 * Escape закрывает меню и возвращает фокус на кнопку; клик вне —
 * закрывает без возврата фокуса. Содержимое монтируется только при
 * открытии: тяжёлые виджеты (опрос кэша) не работают впустую.
 */
export function Popover({ label, open, onOpenChange, children }: Props) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const id = useId();
  const [pos, setPos] = useState<{
    top: number;
    left: number;
    minWidth: number;
  } | null>(null);

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    const panel = panelRef.current;
    if (!trigger || !panel) return;
    const r = trigger.getBoundingClientRect();
    const width = Math.min(380, window.innerWidth - 24);
    const left = Math.max(12, Math.min(r.left, window.innerWidth - width - 12));
    const height = panel.offsetHeight || 0;
    const below = r.bottom + 6;
    const top =
      below + height <= window.innerHeight - 12
        ? below
        : Math.max(12, r.top - height - 6);
    setPos({ top, left, minWidth: Math.min(r.width, width) });
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    // Фокус — в диалог: панель в portal, иначе Tab ушёл бы к следующим
    // кнопкам шапки, а не к содержимому меню.
    panelRef.current?.focus({ preventScroll: true });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      onOpenChangeRef.current(false);
      triggerRef.current?.focus();
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      onOpenChangeRef.current(false);
    };
    const onResize = () => place();
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onResize, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onResize, true);
    };
  }, [open, place]);

  return (
    <span className="popover">
      <button
        ref={triggerRef}
        type="button"
        className="popover-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => onOpenChangeRef.current(!open)}
      >
        {label}
        <span className="popover-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={panelRef}
            id={id}
            className="popover-panel"
            role="dialog"
            aria-label={label}
            tabIndex={-1}
            style={{
              top: pos?.top ?? -9999,
              left: pos?.left ?? -9999,
              minWidth: pos?.minWidth,
            }}
          >
            {children}
          </div>,
          document.body,
        )}
    </span>
  );
}
