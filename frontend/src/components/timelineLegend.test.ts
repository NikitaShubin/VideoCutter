// Легенда таймлайна: смысл цветов объясняется в подсказке к полосе, а не
// отдельной строкой интерфейса. Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { TIMELINE_LEGEND, timelineLegendTitle } from "./timelineLegend";

describe("легенда таймлайна", () => {
  it("идёт второй строкой подсказки", () => {
    const title = timelineLegendTitle("Клик — переход по кадрам");
    expect(title.split("\n")).toEqual([
      "Клик — переход по кадрам",
      TIMELINE_LEGEND,
    ]);
  });

  it("называет значения цветов словами", () => {
    for (const meaning of [
      "свободно",
      "фрагмент",
      "фрагмент с комментарием",
      "после текущего кадра",
      "выделено",
      "текущий кадр",
    ]) {
      expect(TIMELINE_LEGEND).toContain(meaning);
    }
  });
});
