// Экран «Фоновые операции»: карточка показывает тип, цель, прогресс,
// статус и действия; действия — отмена, повтор и скачивание результата.
// Запуск: npm test (vitest run).
import { act, createRef } from "react";
import type { Ref } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelJob,
  deleteJob,
  getJobTypes,
  listJobs,
  retryJob,
} from "../api";
import type { JobQuery } from "../api";
import { OperationsPanel } from "./OperationsPanel";
import type { OperationsPanelHandle } from "./OperationsPanel";
import type { Job, JobTypeManifest } from "../types";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listJobs: vi.fn(),
    cancelJob: vi.fn(),
    retryJob: vi.fn(),
    deleteJob: vi.fn(),
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
  {
    key: "system.validate",
    lane: "system",
    label: "Проверка видео",
    result_kind: "object",
    cancellable: true,
    resumable: true,
    retention_days: null,
    params_schema: {},
    has_prerequisites: true,
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

type JobsLoader = (query: JobQuery) => Job[];

async function mount(
  jobs: Job[] | JobsLoader,
  callbacks: {
    onOpenTask?: (id: string) => void;
    onOpenProject?: (id: string) => void;
  } = {},
  ref?: Ref<OperationsPanelHandle>,
) {
  vi.mocked(listJobs).mockImplementation((query = {}) =>
    Promise.resolve({
      jobs: typeof jobs === "function" ? jobs(query) : jobs,
      next: null,
    }),
  );
  vi.mocked(getJobTypes).mockResolvedValue({ types: TYPES });
  vi.mocked(cancelJob).mockResolvedValue(makeJob());
  vi.mocked(retryJob).mockResolvedValue(makeJob({ status: "queued" }));
  vi.mocked(deleteJob).mockResolvedValue({ deleted: "job-1" });

  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<OperationsPanel ref={ref} {...callbacks} />);
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
  vi.mocked(deleteJob).mockReset();
});

afterEach(() => {
  while (mounted.length > 0) mounted.pop()!();
});

describe("экран фоновых операций", () => {
  it("пустая очередь объясняет, что здесь появится", async () => {
    const { host } = await mount([]);
    expect(host.textContent).toContain("Фоновых операций нет");
  });

  it("не выводит на экран пояснение о фоновом выполнении", async () => {
    const { host } = await mount([]);
    expect(host.textContent).not.toContain("Тяжёлые операции");
    expect(host.textContent).not.toContain("переживают перезапуск");
  });

  it("не занимает отдельную строку кнопкой обновления", async () => {
    const { host } = await mount([]);
    expect(host.querySelector(".operations-head")).toBeNull();
    expect(host.textContent).not.toContain("Обновить");
  });

  it("обновление по общей кнопке перечитывает список", async () => {
    const ref = createRef<OperationsPanelHandle>();
    const { host } = await mount(
      [
        makeJob({
          id: "first",
          status: "done",
          display_status: "done",
          result: { files: ["first.mp4"], urls: ["/first"] },
        }),
      ],
      {},
      ref,
    );
    expect(host.textContent).toContain("first.mp4");

    vi.mocked(listJobs).mockResolvedValue({
      jobs: [
        makeJob({
          id: "second",
          status: "done",
          display_status: "done",
          result: { files: ["second.mp4"], urls: ["/second"] },
        }),
      ],
      next: null,
    });
    await act(async () => {
      await ref.current!.refresh();
    });

    expect(host.textContent).toContain("second.mp4");
    expect(host.textContent).not.toContain("first.mp4");
  });

  it("не показывает завершённую проверку видео", async () => {
    const { host } = await mount([
      makeJob({
        id: "validate-1",
        type: "system.validate",
        lane: "system",
        status: "done",
        display_status: "done",
        progress: { current: 1, total: 1, unit: "задача", message: "" },
      }),
      makeJob({
        id: "export-1",
        status: "done",
        display_status: "done",
        result: { files: ["fragment_1.mp4"], urls: ["/export-1"] },
      }),
    ]);

    const cards = host.querySelectorAll(".operation-card");
    expect(cards).toHaveLength(1);
    expect(cards[0].textContent).toContain("Экспорт фрагментов");
    expect(host.textContent).not.toContain("Проверка видео");
  });

  it("ошибка проверки видео остаётся с повтором", async () => {
    const { host } = await mount([
      makeJob({
        type: "system.validate",
        lane: "system",
        status: "error",
        display_status: "error",
        error: { code: "bad_video", message: "Не удалось прочитать видео" },
      }),
    ]);

    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("Проверка видео");
    expect(card.textContent).toContain("Не удалось прочитать видео");
    expect(
      Array.from(card.querySelectorAll("button")).some(
        (button) => button.textContent === "Повторить",
      ),
    ).toBe(true);
  });

  it("из завершённых операций задачи оставляет последнюю", async () => {
    const { host } = await mount([
      makeJob({
        id: "new",
        status: "done",
        display_status: "done",
        created_at: "2026-11-22T12:00:00Z",
        result: { files: ["new.mp4"], urls: ["/new"] },
      }),
      makeJob({
        id: "old",
        status: "done",
        display_status: "done",
        created_at: "2026-11-22T11:00:00Z",
        result: { files: ["old.mp4"], urls: ["/old"] },
      }),
    ]);

    const cards = host.querySelectorAll(".operation-card");
    expect(cards).toHaveLength(1);
    expect(cards[0].textContent).toContain("new.mp4");
    expect(cards[0].textContent).not.toContain("old.mp4");
  });

  it("не показывает завершённую операцию без файла", async () => {
    const { host } = await mount([
      makeJob({
        type: "backup.import",
        lane: "import",
        status: "done",
        display_status: "done",
        result: null,
      }),
    ]);

    expect(host.querySelectorAll(".operation-card")).toHaveLength(0);
  });

  it("завершённый бэкап с файлом остаётся в списке", async () => {
    const { host } = await mount([
      makeJob({
        type: "task.backup",
        lane: "archive",
        status: "done",
        display_status: "done",
        result: { download_name: "cam-2-backup.zip" },
      }),
    ]);

    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("Бэкап задачи");
    expect(card.textContent).toContain("Скачать");
  });

  it("не показывает готовый результат с истёкшим сроком", async () => {
    const { host } = await mount([
      makeJob({
        type: "task.backup",
        status: "done",
        display_status: "done",
        result: { download_name: "old.zip" },
        retention: {
          expires_at: "2000-01-01T00:00:00+00:00",
          keep: false,
        },
      }),
    ]);

    expect(host.querySelectorAll(".operation-card")).toHaveLength(0);
  });

  it("по умолчанию скрывает историю, но показывает её по кнопке", async () => {
    const { host } = await mount([
      makeJob({
        id: "cancelled-1",
        status: "cancelled",
        display_status: "cancelled",
      }),
    ]);
    expect(host.querySelectorAll(".operation-card")).toHaveLength(0);

    const toggle = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent === "Показать историю",
    )!;
    expect(toggle).toBeTruthy();

    act(() => toggle.click());
    expect(host.querySelectorAll(".operation-card")).toHaveLength(1);
    expect(host.textContent).toContain("Отменено");

    act(() => toggle.click());
    expect(host.querySelectorAll(".operation-card")).toHaveLength(0);
  });

  it("не предлагает историю, когда записей нет вовсе", async () => {
    const { host } = await mount([]);
    expect(host.textContent).not.toContain("Показать историю");
  });

  it("в истории не предлагает скачать истёкший бэкап", async () => {
    const { host } = await mount([
      makeJob({
        type: "task.backup",
        status: "done",
        display_status: "done",
        result: { download_name: "old.zip" },
        retention: {
          expires_at: "2000-01-01T00:00:00+00:00",
          keep: false,
        },
      }),
    ]);

    const toggle = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent === "Показать историю",
    )!;
    act(() => toggle.click());

    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("Бэкап задачи");
    expect(card.textContent).not.toContain("Скачать");
  });

  it("активная операция видна, даже если не попала в последнюю сотню", async () => {
    const active = makeJob({ id: "active-1", status: "running" });
    const old = makeJob({
      id: "done-1",
      status: "done",
      display_status: "done",
      result: { files: ["old.mp4"], urls: ["/old"] },
    });

    const { host } = await mount((query) =>
      query.active ? [active] : [old],
    );

    expect(host.querySelectorAll(".operation-card")).toHaveLength(2);
    expect(host.textContent).toContain("Выполняется");
    expect(host.textContent).toContain("old.mp4");
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

  it("у выполняющейся системной операции нет перехода в задачу", async () => {
    const { host } = await mount(
      [
        makeJob({
          type: "system.validate",
          lane: "system",
          status: "running",
          display_status: "running",
        }),
      ],
      { onOpenTask: () => undefined },
    );

    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("cam-1");
    expect(card.querySelector("button.operation-target")).toBeNull();
    expect(
      Array.from(card.querySelectorAll("button")).some(
        (button) => button.textContent === "Открыть задачу",
      ),
    ).toBe(false);
  });

  it("в истории системная операция тоже не ведёт в задачу", async () => {
    const { host } = await mount(
      [
        makeJob({
          id: "validate-1",
          type: "system.validate",
          lane: "system",
          status: "done",
          display_status: "done",
        }),
      ],
      { onOpenTask: () => undefined },
    );

    const toggle = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent === "Показать историю",
    )!;
    act(() => toggle.click());

    const card = host.querySelector(".operation-card")!;
    expect(card.textContent).toContain("Проверка видео");
    expect(card.querySelector("button.operation-target")).toBeNull();
    expect(
      Array.from(card.querySelectorAll("button")).some(
        (button) => button.textContent === "Открыть задачу",
      ),
    ).toBe(false);
  });

  it("у завершённой операции корзина вместо кнопки открытия", async () => {
    const { host } = await mount(
      [
        makeJob({
          status: "done",
          display_status: "done",
          result: { files: ["f.mp4"], urls: ["/f"] },
        }),
      ],
      { onOpenTask: () => undefined },
    );

    const card = host.querySelector(".operation-card")!;
    expect(
      Array.from(card.querySelectorAll("button")).some(
        (button) => button.textContent === "Открыть задачу",
      ),
    ).toBe(false);
    expect(card.querySelector(".operation-delete")).not.toBeNull();
  });

  it("корзина удаляет операцию после подтверждения", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { host } = await mount([
      makeJob({
        status: "done",
        display_status: "done",
        result: { files: ["f.mp4"], urls: ["/f"] },
      }),
    ]);

    const trash = host.querySelector<HTMLButtonElement>(".operation-delete")!;
    expect(trash.getAttribute("aria-label"))
      .toBe("Удалить операцию: Экспорт фрагментов cam-1");
    await act(async () => {
      trash.click();
    });

    expect(deleteJob).toHaveBeenCalledWith("job-1");
    confirm.mockRestore();
  });

  it("отмена подтверждения не удаляет операцию", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { host } = await mount([
      makeJob({
        status: "done",
        display_status: "done",
        result: { files: ["f.mp4"], urls: ["/f"] },
      }),
    ]);

    await act(async () => {
      host.querySelector<HTMLButtonElement>(".operation-delete")!.click();
    });

    expect(deleteJob).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("у активной операции нет корзины", async () => {
    const { host } = await mount([makeJob()]);

    expect(host.querySelector(".operation-delete")).toBeNull();
  });
});
