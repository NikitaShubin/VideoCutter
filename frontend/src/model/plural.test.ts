// Русские числительные в списках: 1 задача / 2 задачи / 5 задач.
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { pluralCount, pluralWord } from "./plural";

const TASKS = ["задача", "задачи", "задач"] as const;

describe("pluralCount", () => {
  it("одна задача — единственное число", () => {
    expect(pluralCount(1, ...TASKS)).toBe("1 задача");
  });

  it("две задачи — форма paucal", () => {
    expect(pluralCount(2, ...TASKS)).toBe("2 задачи");
  });

  it("пять задач — форма множественного", () => {
    expect(pluralCount(5, ...TASKS)).toBe("5 задач");
  });

  it("одиннадцать задач — форма множественного", () => {
    expect(pluralCount(11, ...TASKS)).toBe("11 задач");
  });

  it("двадцать одна задача — единственное число", () => {
    expect(pluralCount(21, ...TASKS)).toBe("21 задача");
  });

  it("ноль задач — форма множественного", () => {
    expect(pluralCount(0, ...TASKS)).toBe("0 задач");
  });
});

describe("pluralWord", () => {
  it("одна задача — единственное число", () => {
    expect(pluralWord(1, ...TASKS)).toBe("задача");
  });

  it("форма paucal без числа", () => {
    expect(pluralWord(3, ...TASKS)).toBe("задачи");
  });

  it("форма множественного без числа", () => {
    expect(pluralWord(7, ...TASKS)).toBe("задач");
  });
});
