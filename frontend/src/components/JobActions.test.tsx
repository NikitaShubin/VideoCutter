// Кнопка фоновой операции живёт весь цикл: запуск → прогресс/отмена →
// готовый результат. Тесты фиксируют то, что видит пользователь, а не
// внутренние состояния компонента.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelJob,
  createJob,
  downloadJobResult,
  getJob,
  uploadJobArchive,
} from "../api";
import type { Job } from "../types";
import { JobAction, JobFileAction } from "./JobActions";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    createJob: vi.fn(),
    getJob: vi.fn(),
    cancelJob: vi.fn(),
    uploadJobArchive: vi.fn(),
    downloadJobResult: vi.fn(),
  };
});

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    type: "backup.task",
    lane: "archive",
    status: "running",
    display_status: "running",
    owner: "local",
    target: { kind: "task", id: "cam-1" },
    params: {},
    priority: 0,
    depends_on: [],
    attempt: 1,
    progress: { current: 1, total: 4, unit: "файлов", message: "" },
    result: null,
    error: null,
    cancel_requested: false,
    created_at: "2026-11-22T10:00:00Z",
    started_at: "2026-11-22T10:00:01Z",
    finished_at: null,
    retention: { expires_at: null, keep: false },
    ...overrides,
  };
}

const mounted: Array<() => void> = [];

async function mount(node: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(node);
  });
  const unmount = () => {
    act(() => root.unmount());
    host.remove();
  };
  mounted.push(unmount);
  return { host, unmount };
}

function button(host: HTMLElement): HTMLButtonElement {
  return host.querySelector<HTMLButtonElement>(".job-action")!;
}

/** Клик с ожиданием микрозадач запуска/отмены. */
async function click(host: HTMLElement): Promise<void> {
  await act(async () => {
    button(host).click();
  });
}

/** Один шаг опроса (интервал 800 мс) вместе с эффектами ответа. */
async function tick(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(800);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(createJob).mockReset();
  vi.mocked(getJob).mockReset();
  vi.mocked(cancelJob).mockReset();
  vi.mocked(uploadJobArchive).mockReset();
  vi.mocked(downloadJobResult).mockReset();
});

afterEach(() => {
  while (mounted.length > 0) mounted.pop()!();
  vi.useRealTimers();
});

describe("кнопка-операция", () => {
  it("ставит задачу с переданным запросом и показывает прогресс", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    const { host } = await mount(
      <JobAction
        label="Собрать бэкап"
        request={() => ({
          type: "backup.task",
          target: { kind: "task", id: "cam-1" },
        })}
      />,
    );
    await click(host);
    expect(createJob).toHaveBeenCalledWith({
      type: "backup.task",
      target: { kind: "task", id: "cam-1" },
    });
    expect(button(host).textContent).toContain("Отменить · 25 %");
    expect(button(host).getAttribute("title")).toBe(
      "Нажмите, чтобы отменить операцию",
    );
    expect(
      host.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow"),
    ).toBe("25");
  });

  it("повторное нажатие во время работы отменяет, а не дублирует задачу", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    vi.mocked(cancelJob).mockResolvedValue(
      makeJob({ status: "cancelled", display_status: "cancelled" }),
    );
    const { host } = await mount(
      <JobAction label="Собрать бэкап" request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })} />,
    );
    await click(host);
    await click(host);
    expect(cancelJob).toHaveBeenCalledWith("job-1");
    expect(createJob).toHaveBeenCalledTimes(1);
    expect(button(host).textContent).toBe("↻ Собрать бэкап");
    expect(host.querySelector('[role="status"]')?.textContent).toBe(
      "Операция отменена",
    );
  });

  it("готовый файл скачивается сам, кнопка скачивает его повторно", async () => {
    const done = makeJob({
      status: "done",
      display_status: "done",
      progress: { current: 4, total: 4, unit: "файлов", message: "" },
      result: { files: ["cam-1-backup.zip"] },
    });
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    vi.mocked(getJob).mockResolvedValue(done);
    const { host } = await mount(
      <JobAction
        label="Собрать бэкап"
        request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })}
        downloadName="cam-1-backup.zip"
      />,
    );
    await click(host);
    await tick();
    expect(downloadJobResult).toHaveBeenCalledWith(
      "job-1",
      "cam-1-backup.zip",
    );
    await click(host);
    expect(downloadJobResult).toHaveBeenCalledTimes(2);
    expect(createJob).toHaveBeenCalledTimes(1);
    expect(host.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("ошибка показывает причину и предлагает повторить запуск", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    vi.mocked(getJob).mockResolvedValue(
      makeJob({
        status: "error",
        display_status: "error",
        error: { code: "ffmpeg", message: "ffmpeg завершился с кодом 1" },
      }),
    );
    const { host } = await mount(
      <JobAction
        label="Собрать бэкап"
        request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })}
      />,
    );
    await click(host);
    await tick();
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(
      "ffmpeg завершился с кодом 1",
    );
    expect(button(host).textContent).toBe("↻ Собрать бэкап");
    await click(host);
    expect(createJob).toHaveBeenCalledTimes(2);
  });

  it("с onError ошибка уходит наверх без дубля в кнопке", async () => {
    const onError = vi.fn();
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    vi.mocked(getJob).mockResolvedValue(
      makeJob({
        status: "error",
        display_status: "error",
        error: { code: "ffmpeg", message: "ffmpeg завершился с кодом 1" },
      }),
    );
    const { host } = await mount(
      <JobAction
        label="Собрать бэкап"
        request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })}
        onError={onError}
      />,
    );
    await click(host);
    await tick();
    expect(onError).toHaveBeenCalledWith("ffmpeg завершился с кодом 1");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(button(host).getAttribute("title")).toBe(
      "ffmpeg завершился с кодом 1 — нажмите, чтобы повторить",
    );
  });

  it("прерванная операция объясняет, что её прервали", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    vi.mocked(getJob).mockResolvedValue(
      makeJob({ status: "interrupted", display_status: "interrupted" }),
    );
    const { host } = await mount(
      <JobAction label="Собрать бэкап" request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })} />,
    );
    await click(host);
    await tick();
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(
      "Операция прервана",
    );
  });

  it("в очереди кнопка честно говорит, что работа ещё не началась", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob({
        status: "queued",
        display_status: "queued",
        progress: { current: 0, total: 0, unit: "", message: "" },
      }),
      deduplicated: false,
    });
    const { host } = await mount(
      <JobAction label="Собрать бэкап" request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })} />,
    );
    await click(host);
    expect(button(host).textContent).toBe("Отменить · в очереди");
  });

  it("во время отмены кнопка заблокирована и не шлёт вторую отмену", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob({
        status: "running",
        display_status: "cancelling",
        cancel_requested: true,
      }),
      deduplicated: false,
    });
    const { host } = await mount(
      <JobAction label="Собрать бэкап" request={() => ({ type: "backup.task", target: { kind: "task", id: "cam-1" } })} />,
    );
    await click(host);
    expect(button(host).textContent).toBe("Отмена…");
    expect(button(host).disabled).toBe(true);
    expect(cancelJob).not.toHaveBeenCalled();
  });

  it("компактная icon-кнопка показывает процент и называет отмену", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob(),
      deduplicated: false,
    });
    const { host } = await mount(
      <JobAction
        label="⤓"
        ariaLabel="Скачать бэкап проекта"
        compact
        request={() => ({
          type: "backup.project",
          target: { kind: "project", id: "p-1" },
        })}
      />,
    );
    await click(host);
    expect(button(host).textContent).toBe("25 %");
    expect(button(host).getAttribute("aria-label")).toContain(
      "Скачать бэкап проекта",
    );
    expect(button(host).getAttribute("aria-label")).toContain(
      "нажмите, чтобы отменить",
    );
  });
});

describe("файловая кнопка-операция", () => {
  function chooseFile(host: HTMLElement, file: File): void {
    const input = host.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, "files", {
      value: [file],
      configurable: true,
    });
    act(() => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("загружает архив и ставит задачу с upload_id", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    vi.mocked(uploadJobArchive).mockResolvedValue({
      id: "up-1",
      name: "annotations.zip",
      size: 3,
      owner: "local",
    });
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob({ type: "annotations.import" }),
      deduplicated: false,
    });
    const { host } = await mount(
      <JobFileAction
        label="Импортировать разметку"
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: "cam-1" },
          params: { upload_id: uploadId },
        })}
      />,
    );
    chooseFile(host, file);
    await act(async () => {});
    expect(uploadJobArchive).toHaveBeenCalledWith(
      file,
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(createJob).toHaveBeenCalledWith({
      type: "annotations.import",
      target: { kind: "task", id: "cam-1" },
      params: { upload_id: "up-1" },
    });
    expect(button(host).textContent).toContain("Отменить · 25 %");
  });

  it("успешный импорт сообщает наверх и возвращает выбор файла", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    const onDone = vi.fn();
    const done = makeJob({
      type: "annotations.import",
      status: "done",
      display_status: "done",
      progress: { current: 1, total: 1, unit: "файлов", message: "" },
      result: { summary: "Фрагментов: 3" },
    });
    vi.mocked(uploadJobArchive).mockResolvedValue({
      id: "up-1",
      name: "annotations.zip",
      size: 3,
      owner: "local",
    });
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob({ type: "annotations.import" }),
      deduplicated: false,
    });
    vi.mocked(getJob).mockResolvedValue(done);
    const { host } = await mount(
      <JobFileAction
        label="Импортировать разметку"
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: "cam-1" },
          params: { upload_id: uploadId },
        })}
        onDone={onDone}
      />,
    );
    chooseFile(host, file);
    await act(async () => {});
    await tick();
    expect(onDone).toHaveBeenCalledWith(done);
    expect(host.querySelector('input[type="file"]')).not.toBeNull();
    expect(button(host).textContent).toBe("✓ Импортировать разметку");
  });

  it("во время импорта кнопка отменяет задачу", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    vi.mocked(uploadJobArchive).mockResolvedValue({
      id: "up-1",
      name: "annotations.zip",
      size: 3,
      owner: "local",
    });
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob({ type: "annotations.import" }),
      deduplicated: false,
    });
    vi.mocked(cancelJob).mockResolvedValue(
      makeJob({
        type: "annotations.import",
        status: "cancelled",
        display_status: "cancelled",
      }),
    );
    const { host } = await mount(
      <JobFileAction
        label="Импортировать разметку"
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: "cam-1" },
          params: { upload_id: uploadId },
        })}
      />,
    );
    chooseFile(host, file);
    await act(async () => {});
    await click(host);
    expect(cancelJob).toHaveBeenCalledWith("job-1");
    expect(createJob).toHaveBeenCalledTimes(1);
  });

  it("загрузку архива можно отменить до постановки задачи", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    vi.mocked(uploadJobArchive).mockImplementation(
      (_file, _onProgress, signal) =>
        new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => {
            reject(new DOMException("Отменено", "AbortError"));
          });
        }),
    );
    const { host } = await mount(
      <JobFileAction
        label="Импортировать разметку"
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: "cam-1" },
          params: { upload_id: uploadId },
        })}
      />,
    );
    chooseFile(host, file);
    await act(async () => {});
    expect(button(host).textContent).toContain("Отменить загрузку");
    await click(host);
    await act(async () => {});
    expect(createJob).not.toHaveBeenCalled();
    expect(host.querySelector('[role="status"]')?.textContent).toBe(
      "Загрузка отменена",
    );
    expect(host.querySelector('input[type="file"]')).not.toBeNull();
  });

  it("ошибка загрузки показывает причину и позволяет выбрать файл снова", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    vi.mocked(uploadJobArchive).mockRejectedValue(
      new Error("Не хватает места на диске"),
    );
    const { host } = await mount(
      <JobFileAction
        label="Импортировать разметку"
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: "cam-1" },
          params: { upload_id: uploadId },
        })}
      />,
    );
    chooseFile(host, file);
    await act(async () => {});
    expect(host.querySelector('[role="alert"]')?.textContent).toBe(
      "Не хватает места на диске",
    );
    expect(button(host).textContent).toBe("↻ Импортировать разметку");
    expect(host.querySelector('input[type="file"]')).not.toBeNull();
  });
});
