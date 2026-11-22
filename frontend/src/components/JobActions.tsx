// Кнопки фоновых операций: запуск, прогресс-бар, отмена и результат.
// Одна кнопка живёт весь жизненный цикл: «Скачать бэкап» → «Собираю 42%» →
// «Отменить» → готовый файл. Пока операция активна, повторное нажатие
// отменяет её, а не запускает вторую.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelJob,
  createJob,
  downloadJobResult,
  getJob,
  listJobs,
  uploadJobArchive,
  type JobRequest,
} from "../api";
import type { Job, JobPrerequisite } from "../types";
import { failureReason } from "../model/errors";
import { formatPercent } from "../model/format";
import { FileButton } from "./FileButton";

const ACTIVE = new Set(["queued", "running"]);
const ACTIVE_CACHE_MS = 2000;

// Все кнопки на экране делят один запрос активных операций: иначе список
// из сотни задач отправил бы сотни одинаковых запросов при загрузке.
let activeJobsCache: { at: number; jobs: Promise<Job[]> } | null = null;

export function resetActiveJobsCache(): void {
  activeJobsCache = null;
}

function loadActiveJobs(): Promise<Job[]> {
  const now = Date.now();
  if (activeJobsCache && now - activeJobsCache.at < ACTIVE_CACHE_MS) {
    return activeJobsCache.jobs;
  }
  const jobs = listJobs({ active: true, limit: 200 })
    .then((body) => body.jobs)
    .catch(() => []);
  activeJobsCache = { at: now, jobs };
  return jobs;
}

async function findActiveJob(
  type: string,
  target: Job["target"],
): Promise<Job | null> {
  const jobs = await loadActiveJobs();
  return jobs.find(
    (job) =>
      job.type === type &&
      job.target.kind === target.kind &&
      job.target.id === target.id,
  ) ?? null;
}

export function isActiveJob(job: Job | null): boolean {
  return job !== null && ACTIVE.has(job.status);
}

function fraction(job: Job | null): number | null {
  if (!job || job.progress.total <= 0) return null;
  return Math.min(1, Math.max(0, job.progress.current / job.progress.total));
}

function percentText(job: Job | null): string | null {
  const frac = fraction(job);
  return frac === null
    ? null
    : formatPercent(Math.round(frac * 100));
}

/** Первая незавершённая предпосылка: почему операция ещё не началась. */
function blocker(job: Job | null): JobPrerequisite | null {
  if (!job || job.status !== "queued") return null;
  return (job.prerequisites ?? []).find((dep) => dep.status !== "done") ?? null;
}

/** Подпись для скринридера и подсказки: что именно происходит. */
function progressLabel(job: Job | null): string {
  if (!job) return "";
  if (job.display_status === "cancelling") return "Отмена";
  const waiting = blocker(job);
  if (waiting) return `Подготовка: ${waiting.label}`;
  if (job.status === "queued") return "В очереди";
  if (job.progress.message) return job.progress.message;
  const pct = percentText(job);
  return pct === null ? "Выполняется…" : `Выполняется ${pct}`;
}

/** Текст активной кнопки: действие, название операции и прогресс. */
function activeText(job: Job | null, compact: boolean, name: string): string {
  if (!job) return "";
  if (compact) {
    if (job.status === "queued" || job.display_status === "cancelling") {
      return "…";
    }
    return percentText(job) ?? "…";
  }
  if (job.display_status === "cancelling") return `Отмена · ${name}…`;
  const waiting = blocker(job);
  if (waiting) {
    return `Отменить · ${name} · ${waiting.label.toLowerCase()}`;
  }
  if (job.status === "queued") return `Отменить · ${name} · в очереди`;
  const pct = percentText(job);
  return pct === null ? `Отменить · ${name}` : `Отменить · ${name} · ${pct}`;
}

function uploadText(frac: number | null, compact: boolean): string {
  const pct = formatPercent(Math.round((frac ?? 0) * 100));
  return compact ? pct : `Отменить загрузку · ${pct}`;
}

interface Runner {
  job: Job | null;
  active: boolean;
  uploading: boolean;
  uploadFrac: number | null;
  error: string;
  notice: string;
  start: (
    request: JobRequest | (() => JobRequest | Promise<JobRequest>),
  ) => Promise<void>;
  attach: (
    request: JobRequest | (() => JobRequest | Promise<JobRequest>),
  ) => Promise<void>;
  startUpload: (
    file: File,
    request: (uploadId: string) => JobRequest,
  ) => Promise<void>;
  cancel: () => Promise<void>;
  reset: () => void;
}

/** Общее ядро: постановка, опрос, отмена и отклик на терминальный статус. */
function useJobRunner(
  onDone: ((job: Job) => void) | undefined,
  onError: ((message: string) => void) | undefined,
): Runner {
  const [job, setJob] = useState<Job | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadFrac, setUploadFrac] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const timer = useRef<number | null>(null);
  const uploadAbort = useRef<AbortController | null>(null);
  const alive = useRef(true);
  // Номер поколения отсекает поздний ответ поиска: если пользователь уже
  // нажал «запустить», старая операция не должна перебить новую.
  const generation = useRef(0);
  const doneRef = useRef(onDone);
  const errorRef = useRef(onError);
  doneRef.current = onDone;
  errorRef.current = onError;

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      clearTimer();
      uploadAbort.current?.abort();
    };
  }, [clearTimer]);

  const finish = useCallback((next: Job) => {
    resetActiveJobsCache();
    setJob(next);
    if (next.status === "done") {
      setError("");
      setNotice("");
      doneRef.current?.(next);
      return;
    }
    if (next.status === "cancelled") {
      setError("");
      setNotice("Операция отменена");
      return;
    }
    if (next.status === "error" || next.status === "interrupted") {
      setNotice("");
      const message =
        next.error?.message ??
        (next.status === "interrupted" ? "Операция прервана" : "Ошибка операции");
      setError(message);
      errorRef.current?.(message);
    }
  }, []);

  const poll = useCallback(
    (id: string) => {
      clearTimer();
      timer.current = window.setTimeout(async () => {
        try {
          const next = await getJob(id);
          if (!alive.current) return;
          setJob(next);
          if (ACTIVE.has(next.status) || next.display_status === "cancelling") {
            poll(id);
          } else {
            finish(next);
          }
        } catch (e) {
          if (!alive.current) return;
          const message = failureReason(e);
          setError(message);
          errorRef.current?.(message);
        }
      }, 800);
    },
    [clearTimer, finish],
  );

  const track = useCallback(
    (next: Job) => {
      setJob(next);
      setError("");
      setNotice("");
      if (ACTIVE.has(next.status) || next.display_status === "cancelling") {
        poll(next.id);
      } else {
        finish(next);
      }
    },
    [finish, poll],
  );

  const start = useCallback(
    async (
      request: JobRequest | (() => JobRequest | Promise<JobRequest>),
    ) => {
      generation.current += 1;
      setError("");
      setNotice("");
      setJob(null);
      setUploading(false);
      try {
        const payload = typeof request === "function" ? await request() : request;
        const body = await createJob(payload);
        if (!alive.current) return;
        resetActiveJobsCache();
        track(body.job);
      } catch (e) {
        if (!alive.current) return;
        const message = failureReason(e);
        setError(message);
        errorRef.current?.(message);
      }
    },
    [track],
  );

  const attach = useCallback(
    async (
      request: JobRequest | (() => JobRequest | Promise<JobRequest>),
    ) => {
      const gen = generation.current;
      try {
        const payload = typeof request === "function" ? await request() : request;
        const existing = await findActiveJob(payload.type, payload.target);
        if (!alive.current || gen !== generation.current || !existing) return;
        track(existing);
      } catch {
        // Подхват — необязательный: сбой поиска оставляет кнопку в покое.
      }
    },
    [track],
  );

  const startUpload = useCallback(
    async (file: File, request: (uploadId: string) => JobRequest) => {
      generation.current += 1;
      setError("");
      setNotice("");
      setJob(null);
      setUploading(true);
      setUploadFrac(0);
      const controller = new AbortController();
      uploadAbort.current = controller;
      try {
        const upload = await uploadJobArchive(
          file,
          (f) => {
            if (alive.current) setUploadFrac(f);
          },
          controller.signal,
        );
        if (!alive.current) return;
        setUploading(false);
        const body = await createJob(request(upload.id));
        if (!alive.current) return;
        track(body.job);
      } catch (e) {
        if (!alive.current) return;
        setUploading(false);
        if (controller.signal.aborted) {
          setNotice("Загрузка отменена");
          return;
        }
        const message = failureReason(e);
        setError(message);
        errorRef.current?.(message);
      } finally {
        if (uploadAbort.current === controller) uploadAbort.current = null;
      }
    },
    [track],
  );

  const cancel = useCallback(async () => {
    if (uploading) {
      uploadAbort.current?.abort();
      return;
    }
    if (!job || !isActiveJob(job)) return;
    generation.current += 1;
    try {
      const next = await cancelJob(job.id);
      if (!alive.current) return;
      resetActiveJobsCache();
      track(next);
    } catch (e) {
      if (!alive.current) return;
      const message = failureReason(e);
      setError(message);
      errorRef.current?.(message);
    }
  }, [job, track, uploading]);

  const reset = useCallback(() => {
    generation.current += 1;
    clearTimer();
    uploadAbort.current?.abort();
    uploadAbort.current = null;
    setJob(null);
    setUploading(false);
    setUploadFrac(null);
    setError("");
    setNotice("");
  }, [clearTimer]);

  return {
    job,
    active: isActiveJob(job) || job?.display_status === "cancelling",
    uploading,
    uploadFrac,
    error,
    notice,
    start,
    attach,
    startUpload,
    cancel,
    reset,
  };
}

function ActionProgress({ job }: { job: Job | null }) {
  if (!isActiveJob(job)) return null;
  const frac = fraction(job);
  if (frac === null) return null;
  return (
    <span
      className="job-action-bar"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(frac * 100)}
      aria-label={progressLabel(job)}
    >
      <span style={{ width: `${Math.round(frac * 100)}%` }} />
    </span>
  );
}

export interface JobActionProps {
  label: string;
  /** Короткое название операции для активной подписи: «бэкап», «разметка». */
  shortLabel?: string;
  title?: string;
  ariaLabel?: string;
  buttonClassName?: string;
  request: () => JobRequest | Promise<JobRequest>;
  downloadName?: string | ((job: Job) => string);
  onDone?: (job: Job) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
  /** Квадратная icon-кнопка: активное состояние показывает только процент. */
  compact?: boolean;
}

/** Кнопка-операция: запуск → прогресс/отмена → скачивание результата. */
export function JobAction({
  label,
  shortLabel,
  title,
  ariaLabel,
  buttonClassName,
  request,
  downloadName,
  onDone,
  onError,
  disabled,
  compact = false,
}: JobActionProps) {
  const runner = useJobRunner(onDone, onError);
  const downloaded = useRef(false);
  const requestRef = useRef(request);
  requestRef.current = request;
  const [downloadError, setDownloadError] = useState("");
  const name = shortLabel ?? label;
  const cancelling = runner.job?.display_status === "cancelling";
  const done = runner.job?.status === "done";
  const failure = runner.error || downloadError;

  useEffect(() => {
    if (disabled) return;
    void runner.attach(requestRef.current);
  }, [disabled, runner.attach]);

  const download = useCallback(
    async (job: Job) => {
      if (!downloadName) return;
      setDownloadError("");
      const filename =
        typeof downloadName === "function" ? downloadName(job) : downloadName;
      try {
        await downloadJobResult(job.id, filename);
      } catch (e) {
        const message = `Не удалось скачать результат: ${failureReason(e)}`;
        setDownloadError(message);
        onError?.(message);
      }
    },
    [downloadName, onError],
  );

  useEffect(() => {
    if (runner.job?.status === "done" && downloadName && !downloaded.current) {
      downloaded.current = true;
      void download(runner.job);
    }
    if (runner.job === null) downloaded.current = false;
  }, [download, downloadName, runner.job]);

  const click = () => {
    if (runner.active) {
      void runner.cancel();
      return;
    }
    if (runner.job?.status === "done" && downloadName) {
      void download(runner.job);
      return;
    }
    setDownloadError("");
    void runner.start(request);
  };

  const text = runner.uploading
    ? uploadText(runner.uploadFrac, compact)
    : runner.active
      ? activeText(runner.job, compact, name)
      : failure || runner.notice
        ? (compact ? "↻" : `↻ ${label}`)
        : done
          ? (compact ? "✓" : `✓ ${label}`)
          : label;

  const titleText = runner.uploading || runner.active
    ? (cancelling
      ? `Отмена · ${name}…`
      : `Нажмите, чтобы отменить: ${name}`)
    : failure
      ? `${failure} — нажмите, чтобы ${
        done ? "скачать ещё раз" : "повторить"
      }`
      : done
        ? (downloadName
          ? "Результат готов — скачать ещё раз"
          : "Готово — можно запустить снова")
        : runner.notice
          ? `${runner.notice} — нажмите, чтобы запустить снова`
          : title;

  const accessibleLabel = compact && failure
    ? `${ariaLabel ?? label}: ${failure}, нажмите, чтобы ${
      done ? "скачать ещё раз" : "повторить"
    }`
    : compact && (runner.active || runner.uploading)
      ? `${ariaLabel ?? label}: ${
        runner.uploading
          ? uploadText(runner.uploadFrac, false)
          : progressLabel(runner.job)
      }, нажмите, чтобы отменить`
      : ariaLabel;

  return (
    <>
      <button
        type="button"
        className={`job-action ${buttonClassName ?? ""}`.trim()}
        title={titleText}
        aria-label={accessibleLabel}
        disabled={disabled || cancelling}
        onClick={click}
      >
        {compact && failure && (
          <span className="job-action-dot" aria-hidden="true" />
        )}
        {text}
        <ActionProgress job={runner.job} />
      </button>
      {!compact && failure && !onError && (
        <span className="job-action-error" role="alert">
          {failure}
        </span>
      )}
      {!compact && runner.notice && (
        <span className="job-action-note" role="status">
          {runner.notice}
        </span>
      )}
    </>
  );
}

export interface JobFileActionProps {
  label: string;
  /** Короткое название операции для активной подписи. */
  shortLabel?: string;
  title?: string;
  ariaLabel?: string;
  buttonClassName?: string;
  accept?: string;
  request: (uploadId: string) => JobRequest;
  onDone?: (job: Job) => void;
  onError?: (message: string) => void;
  disabled?: boolean;
  compact?: boolean;
}

/** Файловая операция: загрузка архива → фоновая задача → прогресс/отмена. */
export function JobFileAction({
  label,
  shortLabel,
  title,
  ariaLabel,
  buttonClassName,
  accept = ".zip,application/zip",
  request,
  onDone,
  onError,
  disabled,
  compact = false,
}: JobFileActionProps) {
  const runner = useJobRunner(onDone, onError);
  const className = `job-action ${buttonClassName ?? ""}`.trim();
  const name = shortLabel ?? label;
  const cancelling = runner.job?.display_status === "cancelling";
  const done = runner.job?.status === "done";

  const accessibleLabel = compact && runner.error
    ? `${ariaLabel ?? label}: ${runner.error}, нажмите, чтобы повторить`
    : compact && (runner.active || runner.uploading)
      ? `${ariaLabel ?? label}: ${
        runner.uploading
          ? uploadText(runner.uploadFrac, false)
          : progressLabel(runner.job)
      }, нажмите, чтобы отменить`
      : ariaLabel;

  if (runner.active || runner.uploading) {
    const text = runner.uploading
      ? uploadText(runner.uploadFrac, compact)
      : activeText(runner.job, compact, name);
    return (
      <>
        <button
          type="button"
          className={className}
          title={cancelling
            ? `Отмена · ${name}…`
            : `Нажмите, чтобы отменить: ${name}`}
          aria-label={accessibleLabel}
          disabled={cancelling}
          onClick={() => void runner.cancel()}
        >
          {text}
          <ActionProgress job={runner.job} />
        </button>
        {!compact && runner.error && !onError && (
          <span className="job-action-error" role="alert">
            {runner.error}
          </span>
        )}
        {!compact && runner.notice && (
          <span className="job-action-note" role="status">
            {runner.notice}
          </span>
        )}
      </>
    );
  }

  const text = done
    ? (compact ? "✓" : `✓ ${label}`)
    : runner.error || runner.notice
      ? (compact ? "↻" : `↻ ${label}`)
      : label;
  const titleText = done
    ? "Готово — можно выбрать файл ещё раз"
    : runner.error
      ? `${runner.error} — нажмите, чтобы повторить`
      : runner.notice
        ? `${runner.notice} — нажмите, чтобы выбрать файл снова`
        : title;

  return (
    <>
      <FileButton
        className={className}
        accept={accept}
        disabled={disabled}
        title={titleText}
        ariaLabel={accessibleLabel}
        onFile={(file) => void runner.startUpload(file, request)}
      >
        {compact && runner.error && (
          <span className="job-action-dot" aria-hidden="true" />
        )}
        {text}
      </FileButton>
      {!compact && runner.error && !onError && (
        <span className="job-action-error" role="alert">
          {runner.error}
        </span>
      )}
      {!compact && runner.notice && (
        <span className="job-action-note" role="status">
          {runner.notice}
        </span>
      )}
    </>
  );
}
