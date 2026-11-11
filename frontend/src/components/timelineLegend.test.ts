// Легенда таймлайна: смысл цветов объясняется в подсказке к полосе, а не
// отдельной строкой интерфейса. Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { TIMELINE_LEGEND, timelineLegendTitle } from "./timelineLegend";

describe("легенда таймлайна", () => {
  it("идёт после первой строки подсказки", () => {
    const title = timelineLegendTitle("Клик — переход по кадрам");
    expect(title.split("\n")[0]).toBe("Клик — переход по кадрам");
    expect(title.endsWith(`\n${TIMELINE_LEGEND}`)).toBe(true);
  });

  it("называет значения цветов словами", () => {
    for (const meaning of [
      "свободно",
      "фрагмент",
      "фрагмент с комментарием",
      "после текущего кадра",
      "выделено поверх свободного",
      "выделено поверх фрагмента",
      "текущий кадр",
    ]) {
      expect(TIMELINE_LEGEND).toContain(meaning);
    }
  });

  it("каждый цвет — отдельной строкой с образцом", () => {
    const lines = TIMELINE_LEGEND.split("\n");
    expect(lines[0]).toBe("Цвета:");
    for (const [swatch, meaning] of [
      ["🟩", "зелёный — свободно"],
      ["🟥", "красный — фрагмент"],
      ["🟧", "оранжевый — фрагмент с комментарием"],
      ["⬛", "затемнённый — после текущего кадра"],
      ["🟦", "бирюзовый — выделено поверх свободного"],
      ["🟪", "пурпурный — выделено поверх фрагмента"],
      ["⬜", "белый — текущий кадр"],
    ]) {
      expect(lines).toContain(`${swatch} ${meaning}`);
    }
    expect(lines).toHaveLength(8);
  });
});
