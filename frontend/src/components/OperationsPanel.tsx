// Экран фоновых операций: очередь, прогресс, отмена, повтор и результат.
// Опрос адаптивный (2 с при активных, 15 с в покое, пауза во фоне), чтобы
// не грузить сервер на длинных задачах.
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import {
  cancelJob,
  deleteJob,
  getJobTypes,
  listJobs,
  retryJob,
  setJobHidden,
  withTokenQuery,
} from "../api";
import { failureReason } from "../model/errors";
import { pluralCount } from "../model/plural";
import type { Job, JobTypeManifest } from "../types";

const ACTIVE = new Set(["queued", "running"]);
const NEEDS_ATTENTION = new Set(["error", "interrupted"]);

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

function targetKey(job: Job): string {
  return `${job.target.kind}:${job.target.id}`;
}

function targetLabel(job: Job): string {
  return job.target.kind === "task" || job.target.kind === "project"
    ? job.target.id
    : `${job.target.kind}:${job.target.id}`;
}

// Системная операция — подготовка задачи, а не работа с её содержимым:
// пока она идёт, задача ещё не открывается, а после успеха карточка
// скрыта. Ссылка вела бы в заблокированную задачу или исчезала бы
// вместе с карточкой, поэтому у system.* перехода в задачу нет.
function targetIsNavigable(job: Job): boolean {
  return job.lane !== "system";
}

function EyeIcon({ off = false }: { off?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {off ? (
        <>
          <path d="M3 3l18 18" />
          <path d="M10.6 10.6a3 3 0 0 0 4.2 4.2" />
          <path d="M9.9 5.2A10.7 10.7 0 0 1 12 5c6.5 0 10 7 10 7a17.8 17.8 0 0 1-3.2 4.2" />
          <path d="M6.1 6.1A17.3 17.3 0 0 0 2 12s3.5 7 10 7a10.7 10.7 0 0 0 4.9-1.2" />
        </>
      ) : (
        <>
          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
          <circle cx="12" cy="12" r="3" />
        </>
      )}
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v5M14 11v5" />
    </svg>
  );
}

function mergeJobs(...groups: Job[][]): Job[] {
  const seen = new Set<string>();
  const merged: Job[] = [];
  for (const group of groups) {
    for (const job of group) {
      if (seen.has(job.id)) continue;
      seen.add(job.id);
      merged.push(job);
    }
  }
  return merged;
}

function resultExpired(job: Job, now: number): boolean {
  const raw = job.retention?.expires_at;
  if (!raw) return false;
  const time = Date.parse(raw);
  return Number.isFinite(time) && time <= now;
}

// Результат бывает двух форм: список файлов у экспорта и один файл
// (download_name) у бэкапа. Проверять только files нельзя — готовый
// бэкап исчезал бы из списка сразу после завершения.
function hasResultFile(job: Job): boolean {
  const result = job.result;
  if (!result) return false;
  if ((result.files?.length ?? 0) > 0) return true;
  return Boolean(result.download_name || result.download_path);
}

// Экран — не журнал всех записей: активная работа и ошибки видны всегда,
// а из завершённых остаётся только последняя операция по каждой задаче.
// Успешные system.* не показываются вовсе: они уже отражены в состоянии
// задачи, а список нужен, чтобы забрать результат или вернуться к работе.
export function visibleJobs(
  jobs: Job[],
  now = Date.now(),
  showAll = false,
): Job[] {
  if (showAll) return jobs;
  const active: Job[] = [];
  const latestTerminal = new Map<string, Job>();
  for (const job of jobs) {
    if (ACTIVE.has(job.status)) {
      active.push(job);
      continue;
    }
    if (job.status === "done" && job.lane === "system") continue;
    const key = targetKey(job);
    if (!latestTerminal.has(key)) latestTerminal.set(key, job);
  }
  const terminal = [...latestTerminal.values()].filter((job) => {
    if (NEEDS_ATTENTION.has(job.status)) return true;
    if (job.status !== "done") return false;
    if (!hasResultFile(job)) return false;
    return !resultExpired(job, now);
  });
  const visible = new Set([...active, ...terminal].map((job) => job.id));
  return jobs.filter((job) => visible.has(job.id) && !job.hidden);
}

export interface OperationsPanelHandle {
  /** Перечитать список по запросу общей кнопки синхронизации. */
  refresh: () => Promise<void>;
}

interface OperationsPanelProps {
  onOpenTask?: (id: string) => void;
  onOpenProject?: (id: string) => void;
}

export const OperationsPanel = forwardRef<
  OperationsPanelHandle,
  OperationsPanelProps
>(function OperationsPanel({ onOpenTask, onOpenProject }, ref) {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [types, setTypes] = useState<Record<string, JobTypeManifest>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
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

  const fetchJobs = useCallback(async (): Promise<Job[]> => {
    const [activePage, recentPage] = await Promise.all([
      listJobs({ active: true, limit: 100 }),
      listJobs({ limit: 100 }),
    ]);
    return mergeJobs(activePage.jobs, recentPage.jobs);
  }, []);

  const applyJobs = useCallback((next: Job[]) => {
    activeRef.current = next.some((job) => ACTIVE.has(job.status));
    setJobs(next);
    setError("");
  }, []);

  const refreshJobs = useCallback(async () => {
    applyJobs(await fetchJobs());
  }, [applyJobs, fetchJobs]);

  useImperativeHandle(
    ref,
    () => ({ refresh: refreshJobs }),
    [refreshJobs],
  );

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
        const next = await fetchJobs();
        if (!alive) return;
        applyJobs(next);
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
  }, [applyJobs, fetchJobs]);

  const runAction = async (id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await action();
      await refreshJobs();
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

  const remove = (job: Job) => {
    const kind = resultKind(job);
    const consequence =
      kind === "file"
        ? "Запись операции и готовый архив будут удалены."
        : kind === "files"
          ? "Запись операции будет удалена; экспортированные фрагменты останутся."
          : "Запись операции будет удалена. Результат этой операции не хранится.";
    const ok = window.confirm(`Удалить операцию из истории?\n${consequence}`);
    if (!ok) return;
    void runAction(job.id, () => deleteJob(job.id));
  };

  const shown = jobs === null
    ? null
    : visibleJobs(jobs, Date.now(), showAll);

  return (
    <div className="operations-panel">
      {error && (
        <div className="pair-edit-error" role="alert">
          {error}
        </div>
      )}

      {shown === null && <div className="operations-empty">Загрузка…</div>}

      {shown !== null && shown.length === 0 && (
        <div className="operations-empty">
          {jobs !== null && jobs.length > 0
            ? "Активных операций нет. Завершённые записи — в истории."
            : "Фоновых операций нет. Запустите экспорт или бэкап — они появятся здесь."}
        </div>
      )}

      {shown !== null && shown.length > 0 && (
        <ul className="operation-list" aria-label="Фоновые операции">
          {shown.map((job) => {
            const total = job.progress.total;
            const current = job.progress.current;
            const percent =
              total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
            const active = ACTIVE.has(job.status) || job.display_status === "cancelling";
            const files = resultKind(job) === "files" ? exportFiles(job) : [];
            const expired = resultExpired(job, Date.now());
            const canDownload =
              job.status === "done" && resultKind(job) === "file" && !expired;
            const waiting = job.status === "queued"
              ? (job.prerequisites ?? []).filter((dep) => dep.status !== "done")
              : [];
            const navigable = targetIsNavigable(job);
            return (
              <li key={job.id} className={`operation-card status-${job.display_status}`}>
                <div className="operation-main">
                  <div className="operation-title">
                    <span className="operation-type">{label(job)}</span>
                    {job.target.kind === "task" && onOpenTask && navigable ? (
                      <button
                        className="operation-target"
                        onClick={() => onOpenTask(job.target.id)}
                        title="Открыть задачу"
                      >
                        {job.target.id}
                      </button>
                    ) : job.target.kind === "project" && onOpenProject && navigable ? (
                      <button
                        className="operation-target"
                        onClick={() => onOpenProject(job.target.id)}
                        title="Открыть проект"
                      >
                        {job.target.id}
                      </button>
                    ) : (
                      <span className="operation-target">{targetLabel(job)}</span>
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
                  {!active && !showAll && (
                    <button
                      className="btn operation-icon operation-hide"
                      disabled={busy === job.id}
                      onClick={() =>
                        void runAction(job.id, () => setJobHidden(job.id, true))
                      }
                      title="Скрыть из списка"
                      aria-label={`Скрыть операцию: ${label(job)} ${job.target.id}`}
                    >
                      <EyeIcon off />
                    </button>
                  )}
                  {!active && showAll && job.hidden && (
                    <button
                      className="btn operation-icon operation-show"
                      disabled={busy === job.id}
                      onClick={() =>
                        void runAction(job.id, () => setJobHidden(job.id, false))
                      }
                      title="Вернуть в список"
                      aria-label={`Показать операцию: ${label(job)} ${job.target.id}`}
                    >
                      <EyeIcon />
                    </button>
                  )}
                  {!active && (
                    <button
                      className="btn operation-icon operation-delete"
                      disabled={busy === job.id}
                      onClick={() => remove(job)}
                      title="Удалить операцию"
                      aria-label={`Удалить операцию: ${label(job)} ${job.target.id}`}
                    >
                      <TrashIcon />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {jobs !== null && jobs.length > 0 && (
        <button
          className="operations-history"
          aria-pressed={showAll}
          onClick={() => setShowAll((value) => !value)}
        >
          {showAll ? "Скрыть историю" : "Показать историю"}
        </button>
      )}
    </div>
  );
});
