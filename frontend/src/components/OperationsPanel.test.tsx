// Экран «Фоновые операции»: карточка показывает тип, цель, прогресс,
// статус и действия; действия — отмена, повтор и скачивание результата.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelJob,
  getJobTypes,
  listJobs,
  retryJob,
} from "../api";
import { OperationsPanel } from "./OperationsPanel";
import type { Job, JobTypeManifest } from "../types";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listJobs: vi.fn(),
    cancelJob: vi.fn(),
    retryJob: vi.fn(),
    getJobTypes: vi.fn(),
  };
});

const TYPES: JobTypeManifest[] = [
  {
    key: "export.fragments",
    lane: "export",
    label: "Экспорт фрагментов",
    result_kind: "files",
    cancellable: true,
    resumable: false,
    retention_days: null,
    params_schema: {},
    has_prerequisites: false,
  },
  {
    key: "task.backup",
    lane: "archive",
    label: "Бэкап задачи",
    result_kind: "file",
    cancellable: true,
    resumable: false,
    retention_days: 7,
    params_schema: {},
    has_prerequisites: false,
  },
];

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    type: "export.fragments",
    lane: "export",
    status: "running",
    display_status: "running",
    owner: "local",
    target: { kind: "task", id: "cam-1" },
    params: {},
    priority: 0,
    depends_on: [],
    attempt: 1,
    progress: { current: 2, total: 10, unit: "файлов", message: "" },
    result: null,
    error: null,
    cancel_requested: false,
    created_at: "2026-11-22T10:00:00Z",
    started_at: "2026-11-22T10:00:01Z",
    finished_at: null,
    retention: { expires_at: null, keep: true },
    ...overrides,
  };
}

const mounted: Array<() => void> = [];

async function mount(
  jobs: Job[],
  callbacks: {
    onOpenTask?: (id: string) => void;
    onOpenProject?: (id: string) => void;
  } = {},
) {
  vi.mocked(listJobs).mockResolvedValue({ jobs, next: null });
  vi.mocked(getJobTypes).mockResolvedValue({ types: TYPES });
  vi.mocked(cancelJob).mockResolvedValue(makeJob());
  vi.mocked(retryJob).mockResolvedValue(makeJob({ status: "queued" }));

  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<OperationsPanel {...callbacks} />);
  });
  const unmount = () => {
    const i = mounted.indexOf(unmount);
    if (i >= 0) mounted.splice(i, 1);
    act(() => root.unmount());
    host.remove();
  };
  mounted.push(unmount);
  return { host, unmount };
}

beforeEach(() => {
  vi.mocked(listJobs).mockReset();
  vi.mocked(getJobTypes).mockReset();
  vi.mocked(cancelJob).mockReset();
  vi.mocked(retryJob).mockReset();
});

afterEach(() => {
  while (mounted.length > 0) mounted.pop()!();
});

describe("экран фоновых операций", () => {
  it("пустая очередь объясняет, что здесь появится", async () => {
    const { host } = await mount([]);
    expect(host.textContent).toContain("Фоновых операций нет");
  });

  it("карточка показывает тип, цель, владельца и прогресс", async () => {
    const { host } = await mount([makeJob()]);
    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("Экспорт фрагментов");
    expect(card.textContent).toContain("cam-1");
    expect(card.textContent).toContain("local");
    expect(card.textContent).toContain("2 / 10 файлов");
  });

  it("очередь показывает проверку, которая держит операцию", async () => {
    const { host } = await mount([
      makeJob({
        status: "queued",
        display_status: "queued",
        progress: { current: 0, total: 0, unit: "", message: "" },
        prerequisites: [
          {
            id: "dep-1",
            type: "system.validate",
            label: "Проверка видео",
            status: "running",
            display_status: "running",
          },
        ],
        blocked_by: ["dep-1"],
      }),
    ]);

    expect(host.querySelector(".operation-prereq")?.textContent).toBe(
      "Подготовка: Проверка видео",
    );
  });

  it("у выполняющейся операции есть отмена", async () => {
    const { host } = await mount([makeJob()]);
    const cancel = host.querySelector<HTMLButtonElement>(".operation-actions .btn")!;
    expect(cancel.textContent).toBe("Отменить");
    await act(async () => {
      cancel.click();
    });
    expect(cancelJob).toHaveBeenCalledWith("job-1");
  });

  it("ошибка показывает причину и кнопку повтора", async () => {
    const { host } = await mount([
      makeJob({
        status: "error",
        display_status: "error",
        error: { code: "ffmpeg", message: "ffmpeg завершился с кодом 1" },
      }),
    ]);
    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("ffmpeg завершился с кодом 1");
    const retry = Array.from(
      card.querySelectorAll<HTMLButtonElement>(".operation-actions .btn"),
    ).find((btn) => btn.textContent === "Повторить")!;
    await act(async () => {
      retry.click();
    });
    expect(retryJob).toHaveBeenCalledWith("job-1");
  });

  it("готовый файл можно скачать", async () => {
    const { host } = await mount([
      makeJob({
        type: "task.backup",
        status: "done",
        display_status: "done",
        result: {
          files: ["task_cam-1.zip"],
          urls: ["/api/v1/jobs/job-1/download"],
        },
      }),
    ]);
    const download = host.querySelector<HTMLAnchorElement>(
      '.operation-actions a[download]',
    )!;
    expect(download.textContent).toBe("Скачать");
    expect(download.getAttribute("href")).toBe("/api/v1/jobs/job-1/download");
  });

  it("цель-задача открывается по кнопке", async () => {
    const opened: string[] = [];
    const { host } = await mount([makeJob()], {
      onOpenTask: (id) => opened.push(id),
    });
    const target = host.querySelector<HTMLButtonElement>(".operation-target")!;
    act(() => target.click());
    expect(opened).toEqual(["cam-1"]);
  });
});
