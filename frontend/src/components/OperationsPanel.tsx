// Экран фоновых операций: очередь, прогресс, отмена, повтор и результат.
// Опрос адаптивный (2 с при активных, 15 с в покое, пауза во фоне), чтобы
// не грузить сервер на длинных задачах.
import { useEffect, useRef, useState } from "react";
import {
  cancelJob,
  getJobTypes,
  listJobs,
  retryJob,
  withTokenQuery,
} from "../api";
import { failureReason } from "../model/errors";
import { pluralCount } from "../model/plural";
import type { Job, JobTypeManifest } from "../types";

const ACTIVE = new Set(["queued", "running"]);

const STATUS_LABEL: Record<string, string> = {
  queued: "В очереди",
  running: "Выполняется",
  cancelling: "Отмена…",
  done: "Готово",
  error: "Ошибка",
  cancelled: "Отменено",
  interrupted: "Прервано",
};

function statusLabel(job: Job): string {
  return STATUS_LABEL[job.display_status] ?? job.status;
}

function jobTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function progressText(job: Job): string {
  const { current, total, unit, message } = job.progress;
  if (message) return message;
  if (total <= 0) return "";
  const label = unit ? ` ${unit}` : "";
  return `${current} / ${total}${label}`;
}

function exportFiles(job: Job): Array<{ name: string; url: string }> {
  const names = job.result?.files ?? [];
  const urls = job.result?.urls ?? [];
  return names.map((name, index) => ({ name, url: urls[index] ?? "" }));
}

export function OperationsPanel({
  onOpenTask,
  onOpenProject,
}: {
  onOpenTask?: (id: string) => void;
  onOpenProject?: (id: string) => void;
}) {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [types, setTypes] = useState<Record<string, JobTypeManifest>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const activeRef = useRef(false);

  useEffect(() => {
    let alive = true;
    getJobTypes()
      .then((body) => {
        if (!alive) return;
        setTypes(Object.fromEntries(body.types.map((t) => [t.key, t])));
      })
      .catch(() => {
        /* подписи типов — не критичный путь */
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: number | null = null;

    const schedule = () => {
      if (!alive) return;
      const delay = document.hidden ? 15000 : activeRef.current ? 2000 : 15000;
      timer = window.setTimeout(() => {
        void tick();
      }, delay);
    };

    const tick = async () => {
      try {
        const page = await listJobs({ limit: 100 });
        if (!alive) return;
        activeRef.current = page.jobs.some((job) => ACTIVE.has(job.status));
        setJobs(page.jobs);
        setError("");
      } catch (e) {
        if (!alive) return;
        activeRef.current = false;
        setError(`Не удалось получить операции: ${failureReason(e)}`);
      }
      schedule();
    };

    const onVisible = () => {
      if (document.hidden) return;
      if (timer !== null) window.clearTimeout(timer);
      void tick();
    };

    void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      if (timer !== null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reloadKey]);

  const runAction = async (id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await action();
      setReloadKey((key) => key + 1);
    } catch (e) {
      setError(`Не удалось выполнить действие: ${failureReason(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const label = (job: Job): string =>
    types[job.type]?.label ?? job.type;
  const resultKind = (job: Job): string =>
    types[job.type]?.result_kind ?? "none";

  return (
    <div className="operations-panel">
      <div className="operations-head">
        <p className="operations-hint">
          Тяжёлые операции выполняются в фоне и переживают перезапуск сервиса.
        </p>
        <button
          className="btn"
          onClick={() => setReloadKey((key) => key + 1)}
          title="Обновить список операций"
        >
          Обновить
        </button>
      </div>

      {error && (
        <div className="pair-edit-error" role="alert">
          {error}
        </div>
      )}

      {jobs === null && <div className="operations-empty">Загрузка…</div>}

      {jobs !== null && jobs.length === 0 && (
        <div className="operations-empty">
          Фоновых операций нет. Запустите экспорт или бэкап — они появятся
          здесь.
        </div>
      )}

      {jobs !== null && jobs.length > 0 && (
        <ul className="operation-list" aria-label="Фоновые операции">
          {jobs.map((job) => {
            const total = job.progress.total;
            const current = job.progress.current;
            const percent =
              total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
            const active = ACTIVE.has(job.status) || job.display_status === "cancelling";
            const files = resultKind(job) === "files" ? exportFiles(job) : [];
            const canDownload =
              job.status === "done" && resultKind(job) === "file";
            const waiting = job.status === "queued"
              ? (job.prerequisites ?? []).filter((dep) => dep.status !== "done")
              : [];
            return (
              <li key={job.id} className={`operation-card status-${job.display_status}`}>
                <div className="operation-main">
                  <div className="operation-title">
                    <span className="operation-type">{label(job)}</span>
                    {job.target.kind === "task" && onOpenTask ? (
                      <button
                        className="operation-target"
                        onClick={() => onOpenTask(job.target.id)}
                        title="Открыть задачу"
                      >
                        {job.target.id}
                      </button>
                    ) : job.target.kind === "project" && onOpenProject ? (
                      <button
                        className="operation-target"
                        onClick={() => onOpenProject(job.target.id)}
                        title="Открыть проект"
                      >
                        {job.target.id}
                      </button>
                    ) : (
                      <span className="operation-target">
                        {job.target.kind}:{job.target.id}
                      </span>
                    )}
                    <span
                      className={`operation-status status-${job.display_status}`}
                    >
                      {statusLabel(job)}
                    </span>
                  </div>

                  <div className="operation-meta">
                    <span>{job.owner}</span>
                    <span>создана {jobTime(job.created_at)}</span>
                    {job.finished_at && (
                      <span>завершена {jobTime(job.finished_at)}</span>
                    )}
                    {job.attempt > 1 && <span>попытка {job.attempt}</span>}
                  </div>

                  {waiting.length > 0 && (
                    <div className="operation-prereq">
                      Подготовка: {waiting.map((dep) => dep.label).join(", ")}
                    </div>
                  )}

                  {total > 0 && (
                    <div
                      className="operation-progress"
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={total}
                      aria-valuenow={current}
                      aria-label={`${label(job)}: ${progressText(job)}`}
                    >
                      <div
                        className="operation-progress-bar"
                        style={{ width: `${percent}%` }}
                      />
                      <span className="operation-progress-text">
                        {progressText(job)}
                      </span>
                    </div>
                  )}

                  {job.error && job.status === "error" && (
                    <div className="operation-error" role="alert">
                      {job.error.message}
                    </div>
                  )}

                  {files.length > 0 && (
                    <details className="operation-files">
                      <summary>
                        {pluralCount(files.length, "файл", "файла", "файлов")}
                      </summary>
                      <ul>
                        {files.map((file) => (
                          <li key={file.url || file.name}>
                            {file.url ? (
                              <a
                                href={withTokenQuery(file.url)}
                                download
                              >
                                {file.name}
                              </a>
                            ) : (
                              file.name
                            )}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>

                <div className="operation-actions">
                  {active && job.display_status !== "cancelling" && (
                    <button
                      className="btn"
                      disabled={busy === job.id}
                      onClick={() =>
                        void runAction(job.id, () => cancelJob(job.id))
                      }
                    >
                      Отменить
                    </button>
                  )}
                  {!active && job.status !== "done" && (
                    <button
                      className="btn"
                      disabled={busy === job.id}
                      onClick={() =>
                        void runAction(job.id, () => retryJob(job.id))
                      }
                    >
                      Повторить
                    </button>
                  )}
                  {canDownload && (
                    <a
                      className="btn"
                      href={withTokenQuery(`/api/v1/jobs/${job.id}/download`)}
                      download
                    >
                      Скачать
                    </a>
                  )}
                  {job.target.kind === "task" && onOpenTask && (
                    <button
                      className="btn"
                      onClick={() => onOpenTask(job.target.id)}
                    >
                      Открыть задачу
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
