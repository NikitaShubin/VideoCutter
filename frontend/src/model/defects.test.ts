import { describe, expect, it } from "vitest";
import { defectHint, isRoleBroken, projectDefects } from "./defects";
import type { VideoPair } from "../types";

function makePair(overrides: Partial<VideoPair> = {}): VideoPair {
  return {
    id: "cam-2",
    name: "",
    source_name: "source.mp4",
    preview_name: "preview.mp4",
    unassigned_name: null,
    total_frames: 0,
    width: 0,
    height: 0,
    fps: 0,
    source_frames: 0,
    preview_frames: 0,
    source_skipped: 0,
    preview_skipped: 0,
    visible_match: false,
    pair_warning: "",
    fragments: [],
    position: 0,
    updated_at: 0,
    video_ver: "",
    quality: 78,
    scale: 0.75,
    indexing: false,
    indexing_progress: null,
    broken: true,
    error: "boom",
    error_role: null,
    created_at: null,
    last_opened_at: null,
    project_id: null,
    export: null,
    ...overrides,
  };
}

describe("роль сломанного файла", () => {
  it("здоровая задача — ни одна роль не сломана", () => {
    expect(isRoleBroken(null, "source")).toBe(false);
    expect(isRoleBroken(null, "preview")).toBe(false);
  });

  it("источник сломан — сломана только роль источника", () => {
    expect(isRoleBroken("source", "source")).toBe(true);
    expect(isRoleBroken("source", "preview")).toBe(false);
  });

  it("превью сломано — сломана только роль превью", () => {
    expect(isRoleBroken("preview", "preview")).toBe(true);
    expect(isRoleBroken("preview", "source")).toBe(false);
  });

  it("оба файла сломаны — сломаны обе роли", () => {
    expect(isRoleBroken("both", "source")).toBe(true);
    expect(isRoleBroken("both", "preview")).toBe(true);
  });
});

describe("подсказка о поломке", () => {
  it("для здоровой задачи подсказки нет", () => {
    expect(defectHint(null)).toBe("");
  });

  it("коротко говорит, что файл не читается", () => {
    expect(defectHint("source")).toContain("не читается");
    expect(defectHint("preview")).toContain("не читается");
    expect(defectHint("both")).toContain("не читается");
  });

  it("короткая — ряд с именем и кнопками не должен ломаться", () => {
    expect(defectHint("source").length).toBeLessThan(20);
  });
});

describe("битые задачи по проектам", () => {
  it("проект без битых задач не помечен", () => {
    const pairs = [makePair({ broken: false, project_id: "p1" })];
    expect(projectDefects(pairs).has("p1")).toBe(false);
  });

  it("битая задача помечает свой проект", () => {
    const pairs = [makePair({ project_id: "p1", name: "Камера 2" })];
    expect(projectDefects(pairs).get("p1")).toEqual(["Камера 2"]);
  });

  it("standalone-задача (без проекта) никого не помечает", () => {
    const pairs = [makePair({ project_id: null })];
    expect(projectDefects(pairs).size).toBe(0);
  });

  it("несколько битых задач проекта собираются в список", () => {
    const pairs = [
      makePair({ id: "a", name: "А", project_id: "p1" }),
      makePair({ id: "b", name: "Б", project_id: "p1" }),
    ];
    expect(projectDefects(pairs).get("p1")).toEqual(["А", "Б"]);
  });

  it("пустой список задач не ломает разметку", () => {
    expect(projectDefects(null).size).toBe(0);
    expect(projectDefects([]).size).toBe(0);
  });
});
