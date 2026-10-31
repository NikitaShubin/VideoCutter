// Русская локаль чисел: десятичная запятая, неразрывный пробел в тысячах
// и перед знаком процента. Один формат на весь UI, чтобы «59.0%» больше
// нигде не протекало.
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { formatNumber, formatPercent } from "./format";

describe("formatNumber", () => {
  it("тысячи разделяются неразрывным пробелом", () => {
    expect(formatNumber(1234)).toBe("1\u00A0234");
  });

  it("дробная часть — с запятой", () => {
    expect(formatNumber(12.5, 1)).toBe("12,5");
  });

  it("фиксированное число знаков не теряется", () => {
    expect(formatNumber(20, 1)).toBe("20,0");
  });
});

describe("formatPercent", () => {
  it("целый процент — с пробелом перед знаком", () => {
    expect(formatPercent(50)).toBe("50 %");
  });

  it("дробный процент — запятая и пробел", () => {
    expect(formatPercent(50.7, 1)).toBe("50,7 %");
  });
});
