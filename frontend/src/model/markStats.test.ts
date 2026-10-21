// Статистика выделенных кадров: сколько выделено и какая это доля от
// общего числа кадров. Контракт — для редактора, списка задач и проектов.
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { aggregateMarkStats, markedFrameStats, markedLabel } from "./markStats";

describe("markedFrameStats", () => {
  it("без разметки выделено ноль", () => {
    expect(markedFrameStats([], 100).marked).toBe(0);
  });

  it("границы фрагмента включительные: один кадр", () => {
    expect(markedFrameStats([{ start: 5, end: 5 }], 100).marked).toBe(1);
  });

  it("несколько фрагментов суммируются", () => {
    const stats = markedFrameStats(
      [
        { start: 0, end: 9 },
        { start: 20, end: 29 },
      ],
      100,
    );
    expect(stats.marked).toBe(20);
  });

  it("доля выделенных в процентах", () => {
    expect(markedFrameStats([{ start: 0, end: 19 }], 100).percent).toBe(20);
  });

  it("процент округляется до одного знака", () => {
    expect(markedFrameStats([{ start: 0, end: 0 }], 3).percent).toBe(33.3);
  });

  it("общее число кадров неизвестно — доли нет", () => {
    expect(markedFrameStats([{ start: 0, end: 9 }], 0).percent).toBeNull();
  });
});

describe("markedLabel", () => {
  it("выделенного нет — метка скрыта", () => {
    expect(markedLabel(markedFrameStats([], 100))).toBe("");
  });

  it("метка: выделено K из N кадров (X%)", () => {
    expect(markedLabel(markedFrameStats([{ start: 0, end: 19 }], 100))).toBe(
      "выделено 20 из 100 кадров (20.0%)",
    );
  });

  it("один кадр в знаменателе — родительный", () => {
    expect(markedLabel(markedFrameStats([{ start: 0, end: 0 }], 1))).toBe(
      "выделено 1 из 1 кадра (100.0%)",
    );
  });

  it("знаменатель 2-4 — родительный singular", () => {
    expect(markedLabel(markedFrameStats([{ start: 0, end: 0 }], 3))).toBe(
      "выделено 1 из 3 кадра (33.3%)",
    );
  });
});

describe("aggregateMarkStats", () => {
  const two = [
    markedFrameStats([{ start: 0, end: 9 }], 100),
    markedFrameStats([{ start: 0, end: 4 }], 100),
  ];

  it("суммирует выделенные кадры по задачам", () => {
    expect(aggregateMarkStats(two).marked).toBe(15);
  });

  it("суммирует общее число кадров по задачам", () => {
    expect(aggregateMarkStats(two).total).toBe(200);
  });

  it("доля проекта считается от суммы кадров", () => {
    expect(aggregateMarkStats(two).percent).toBe(7.5);
  });

  it("без задач доли нет", () => {
    expect(aggregateMarkStats([]).percent).toBeNull();
  });
});
