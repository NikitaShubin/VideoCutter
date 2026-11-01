/**
 * Свёрнутая легенда цветов таймлайна: значение цвета объясняется рядом с
 * полосой, а не только в справке. По умолчанию закрыта — не отнимает место
 * у кадра и списка, но остаётся одним нажатием.
 */
export function TimelineLegend({ className }: { className?: string }) {
  return (
    <details className={className ? `timeline-legend ${className}` : "timeline-legend"}>
      <summary>Легенда таймлайна</summary>
      <ul>
        <li>
          <span className="legend-swatch legend-free" aria-hidden="true" />
          свободно
        </li>
        <li>
          <span className="legend-swatch legend-fragment" aria-hidden="true" />
          фрагмент
        </li>
        <li>
          <span className="legend-swatch legend-comment" aria-hidden="true" />
          фрагмент с комментарием
        </li>
        <li>
          <span className="legend-swatch legend-viewed" aria-hidden="true" />
          просмотрено
        </li>
        <li>
          <span className="legend-swatch legend-selected" aria-hidden="true" />
          выделено
        </li>
        <li>
          <span className="legend-swatch legend-current" aria-hidden="true" />
          текущий кадр
        </li>
      </ul>
    </details>
  );
}
