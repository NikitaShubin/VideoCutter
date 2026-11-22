// Меню «Данные» ставит фоновые задачи теми же типами, что и общий API:
// разметка и бэкап задачи. Тесты фиксируют связь кнопок с типами задач.
// Запуск: npm test (vitest run).
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createJob, uploadJobArchive } from "../api";
import type { Job } from "../types";
import { DataMenu } from "./DataMenu";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    createJob: vi.fn(),
    uploadJobArchive: vi.fn(),
  };
});

function makeJob(type: string): Job {
  return {
    id: "job-1",
    type,
    lane: "archive",
    status: "queued",
    display_status: "queued",
    owner: "local",
    target: { kind: "task", id: "cam-1" },
    params: {},
    priority: 0,
    depends_on: [],
    attempt: 1,
    progress: { current: 0, total: 0, unit: "", message: "" },
    result: null,
    error: null,
    cancel_requested: false,
    created_at: "2026-11-22T10:00:00Z",
    started_at: null,
    finished_at: null,
    retention: { expires_at: null, keep: false },
  };
}

const mounted: Array<() => void> = [];

async function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<DataMenu pairId="cam-1" />);
  });
  const unmount = () => {
    act(() => root.unmount());
    host.remove();
  };
  mounted.push(unmount);
  return { host };
}

function findButton(host: HTMLElement, label: string): HTMLButtonElement {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent === label,
  )!;
}

beforeEach(() => {
  vi.mocked(createJob).mockReset();
  vi.mocked(uploadJobArchive).mockReset();
});

afterEach(() => {
  while (mounted.length > 0) mounted.pop()!();
});

describe("меню «Данные»", () => {
  it("«Скачать разметку» ставит задачу экспорта разметки", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob("annotations.export"),
      deduplicated: false,
    });
    const { host } = await mount();
    await act(async () => {
      findButton(host, "Скачать разметку").click();
    });
    expect(createJob).toHaveBeenCalledWith({
      type: "annotations.export",
      target: { kind: "task", id: "cam-1" },
    });
  });

  it("«Скачать бэкап» ставит задачу бэкапа задачи", async () => {
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob("backup.task"),
      deduplicated: false,
    });
    const { host } = await mount();
    await act(async () => {
      findButton(host, "Скачать бэкап").click();
    });
    expect(createJob).toHaveBeenCalledWith({
      type: "backup.task",
      target: { kind: "task", id: "cam-1" },
    });
  });

  it("импорт разметки сначала загружает архив, затем ставит задачу", async () => {
    const file = new File(["zip"], "annotations.zip", {
      type: "application/zip",
    });
    vi.mocked(uploadJobArchive).mockResolvedValue({
      id: "up-7",
      name: "annotations.zip",
      size: 3,
      owner: "local",
    });
    vi.mocked(createJob).mockResolvedValue({
      job: makeJob("annotations.import"),
      deduplicated: false,
    });
    const { host } = await mount();
    const input = host.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, "files", {
      value: [file],
      configurable: true,
    });
    act(() => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {});
    expect(uploadJobArchive).toHaveBeenCalledWith(
      file,
      expect.any(Function),
      expect.any(AbortSignal),
    );
    expect(createJob).toHaveBeenCalledWith({
      type: "annotations.import",
      target: { kind: "task", id: "cam-1" },
      params: { upload_id: "up-7" },
    });
  });
});
