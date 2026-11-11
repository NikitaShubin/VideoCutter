/**
 * Легенда таймлайна — данные для видимой плашки в списке и раздела
 * «Таймлайн» в справке. Образцы берут цвета из палитры статусбара, поэтому
 * легенда не может разойтись с тем, что нарисовано на полосе.
 */
import {
  COMMENTED_FILL,
  CURRENT_FRAME_FILL,
  DIM_FILL,
  FRAGMENT_FILL,
  SCHEME_FILLS,
  SELECTION_OVER_FRAGMENT_FILL,
  SELECTION_OVER_FREE_FILL,
  TIMELINE_FREE_FILL,
} from "../model/statusbar";

export interface LegendItem {
  /** CSS-цвет образца; совпадает с цветом полосы. */
  color: string;
  /** Смысл цвета словами — цвет не единственный носитель информации. */
  label: string;
}

export const TIMELINE_LEGEND: readonly LegendItem[] = [
  { color: TIMELINE_FREE_FILL, label: "зелёный — свободно" },
  { color: FRAGMENT_FILL, label: "красный — фрагмент" },
  { color: COMMENTED_FILL, label: "оранжевый — фрагмент с комментарием" },
  { color: DIM_FILL, label: "затемнённый — после текущего кадра" },
  {
    color: SELECTION_OVER_FREE_FILL,
    label: "синий — выделено поверх свободного",
  },
  {
    color: SELECTION_OVER_FRAGMENT_FILL,
    label: "пурпурный — выделено поверх фрагмента",
  },
  { color: CURRENT_FRAME_FILL, label: "белый — текущий кадр" },
];

export const PROGRESS_LEGEND: readonly LegendItem[] = [
  { color: SCHEME_FILLS.upload, label: "синий — отправка файла" },
  { color: SCHEME_FILLS.receipt, label: "бирюзовый — приём сервером" },
  { color: SCHEME_FILLS.indexing, label: "фиолетовый — индексация" },
];
