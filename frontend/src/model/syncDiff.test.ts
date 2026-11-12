// Расхождения между двумя снимками состояния: что изменилось на диске,
// пока сервис смотрел в другую сторону. Правило простое: сравниваем только
// то, что пользователь видит как «состояние задачи» (состав задач, проектов
// и наличие нарезок). Счётчики кадров и прочее — шум.
//
// Запуск: npm test (vitest run).
import { describe, expect, it } from "vitest";
import { diffSync, type SyncSnapshot } from "./syncDiff";
import type { ExportStatus, Project, VideoPair } from "../types";

function pair(
  id: string,
  exportState: ExportStatus["state"] | null = null,
): VideoPair {
  return {
    id,
    name: id,
    export: exportState ? { state: exportState } : null,
  } as VideoPair;
}

function project(id: string): Project {
  return { id, name: id } as Project;
}

function snapshot(
  pairs: VideoPair[] = [],
  projects: Project[] = [],
): SyncSnapshot {
  return { pairs, projects };
}

describe("расхождения состояния", () => {
  it("одинаковые снимки — расхождений нет", () => {
    const before = snapshot([pair("a")], [project("p")]);
    const after = snapshot([pair("a")], [project("p")]);
    expect(diffSync(before, after)).toEqual([]);
  });

  it("появившаяся задача — расхождение", () => {
    const changes = diffSync(
      snapshot([pair("a")]),
      snapshot([pair("a"), pair("b")]),
    );
    expect(changes).toEqual(['Появилась задача «b»']);
  });

  it("исчезнувшая задача — расхождение", () => {
    const changes = diffSync(
      snapshot([pair("a"), pair("b")]),
      snapshot([pair("a")]),
    );
    expect(changes).toEqual(['Задача «b» исчезла']);
  });

  it("пропавшие нарезки — расхождение", () => {
    const changes = diffSync(
      snapshot([pair("a", "done")]),
      snapshot([pair("a")]),
    );
    expect(changes).toEqual(['У задачи «a» пропали нарезки']);
  });

  it("появившиеся нарезки — расхождение", () => {
    const changes = diffSync(
      snapshot([pair("a")]),
      snapshot([pair("a", "done")]),
    );
    expect(changes).toEqual(['У задачи «a» появились нарезки']);
  });

  it("появившийся и исчезнувший проекты — расхождения", () => {
    const changes = diffSync(
      snapshot([], [project("old")]),
      snapshot([], [project("new")]),
    );
    expect(changes).toEqual([
      'Появился проект «new»',
      'Проект «old» исчез',
    ]);
  });
});
