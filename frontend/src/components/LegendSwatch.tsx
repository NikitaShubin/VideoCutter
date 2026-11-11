interface Props {
  /** CSS-цвет, совпадающий с цветом полосы. */
  color: string;
}

/** Размер задаётся здесь, а не содержимым строки: в узком флекс-ряду
 * образец не должен сжиматься в прямоугольник. */
const SWATCH_STYLE = {
  width: 14,
  height: 14,
  flex: "0 0 14px",
} as const;

/** Квадратный образец цвета: одна форма и размер для справки и плашки
 * списка. */
export function LegendSwatch({ color }: Props) {
  return (
    <i
      className="legend-swatch"
      style={{ ...SWATCH_STYLE, background: color }}
      aria-hidden="true"
    />
  );
}
