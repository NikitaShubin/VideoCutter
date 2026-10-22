import type { ReactNode } from "react";

interface Props {
  /** Подпись строки (имя задачи/загрузки). */
  label: ReactNode;
  /** Второй ряд: роли, проект, статистика. */
  meta?: ReactNode;
  /** Пометка под мета: ошибка или предупреждение. */
  warn?: ReactNode;
  /** Фон строки (превью статус-бара). */
  background?: ReactNode;
  /** Кнопки действий справа от каркаса. */
  actions?: ReactNode;
  /** Класс <li> (например "broken"). */
  className?: string;
  /** Содержимое под строкой (панель редактирования). */
  children?: ReactNode;
}

/** Каркас строки списка: рамка `.pair-open` + подписи + кнопки.
 *
 * Единый скелет для состояний, которые не открываются в редактор (загрузка,
 * битая задача). Рамка, отступы и типографика живут на `.pair-open`: строка
 * без него «разъезжается» — рамки нет, подписи теряют стили. Кликабельный
 * вариант (обычная задача) остаётся кнопкой `.pair-open` — CSS поддерживает
 * обе формы одинаково.
 */
export function PairRowFrame({
  label,
  meta,
  warn,
  background,
  actions,
  className,
  children,
}: Props) {
  return (
    <li className={className}>
      <div className="pair-row">
        <div className="pair-open" aria-disabled="true">
          {background}
          <span className="pair-label">{label}</span>
          {meta != null && <span className="pair-meta">{meta}</span>}
          {warn != null && <span className="pair-warn">{warn}</span>}
        </div>
        {actions != null && <div className="pair-actions">{actions}</div>}
      </div>
      {children}
    </li>
  );
}
