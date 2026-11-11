// Легенда таймлайна — данные для видимой плашки в списке и раздела
// «Таймлайн» в справке. Образцы берут цвета из той же палитры, что рисует
// статусбар: легенда не может разойтись с полосой.
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
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
import { PROGRESS_LEGEND, TIMELINE_LEGEND } from "./timelineLegend";

describe("легенда таймлайна", () => {
  it("перечисляет все состояния разметки", () => {
    expect(TIMELINE_LEGEND.map((item) => item.label)).toEqual([
      "зелёный — свободно",
      "красный — фрагмент",
      "оранжевый — фрагмент с комментарием",
      "затемнённый — после текущего кадра",
      "синий — выделено поверх свободного",
      "пурпурный — выделено поверх фрагмента",
      "белый — текущий кадр",
    ]);
  });

  it("берёт образцы из палитры статусбара", () => {
    expect(TIMELINE_LEGEND.map((item) => item.color)).toEqual([
      TIMELINE_FREE_FILL,
      FRAGMENT_FILL,
      COMMENTED_FILL,
      DIM_FILL,
      SELECTION_OVER_FREE_FILL,
      SELECTION_OVER_FRAGMENT_FILL,
      CURRENT_FRAME_FILL,
    ]);
  });

  it("объясняет стадии создания задачи", () => {
    expect(PROGRESS_LEGEND.map((item) => item.label)).toEqual([
      "синий — отправка файла",
      "бирюзовый — приём сервером",
      "фиолетовый — индексация",
    ]);
    expect(PROGRESS_LEGEND.map((item) => item.color)).toEqual([
      SCHEME_FILLS.upload,
      SCHEME_FILLS.receipt,
      SCHEME_FILLS.indexing,
    ]);
  });
});
