export interface Fragment {
  start: number;
  end: number;
  comment?: string;
}

/** Роль сломанного файла задачи: что именно не читается. */
export type DefectRole = "source" | "preview" | "both";

export interface VideoPair {
  id: string;
  /** Отображаемое имя (как task.name в CVAT): дубли разрешены, пусто — id. */
  name: string;
  source_name: string;
  preview_name: string;
  unassigned_name: string | null;
  total_frames: number;
  width: number;
  height: number;
  fps: number;
  source_frames: number;
  preview_frames: number;
  source_skipped: number;
  preview_skipped: number;
  visible_match: boolean;
  pair_warning: string;
  fragments: Fragment[];
  position: number;
  updated_at: number;
  video_ver: string;
  quality: number;
  scale: number;
  indexing: boolean;
  /** Доля готовности индекса 0..1 (null — сборка ещё не отметилась). */
  indexing_progress: number | null;
  broken: boolean;
  error: string;
  /** Какая роль сломана (null — про задачу в целом или всё цело). */
  error_role: DefectRole | null;
  created_at: string | null;
  last_opened_at: string | null;
  /** Проект задачи (null — standalone); состав — реестр проектов. */
  project_id: string | null;
  export: {
    state: string;
    index: number;
    total: number;
    error: string;
  } | null;
}

export interface VideoPairDetail extends VideoPair {}

/** Проект (отдельная сущность, как в CVAT): имя + точка расширения model. */
export interface Project {
  id: string;
  name: string;
  created_at: string | null;
  /** Заглушка будущей модели визуализации/AL — только хранение. */
  model: { id: string | null; kind: string; params: Record<string, unknown> };
  task_count: number;
}

export interface ExportItem {
  index: number;
  filename: string;
  url: string;
}

export interface ExportStatus {
  state: "idle" | "running" | "cancelling" | "done" | "cancelled" | "error";
  index?: number;
  total?: number;
  files?: ExportItem[];
  error?: string;
  /** id фоновой операции, если статус пришёл из общего планировщика. */
  jobId?: string;
}

// ─── Фоновые операции (общий API /api/v1/jobs) ──────────────────────────

export type JobStatus =
  | "queued"
  | "running"
  | "done"
  | "error"
  | "cancelled"
  | "interrupted";

export interface JobTarget {
  kind: string;
  id: string;
  project_id?: string | null;
}

export interface JobProgress {
  current: number;
  total: number;
  unit: string;
  message: string;
}

export interface JobError {
  code: string;
  message: string;
}

export interface JobPrerequisite {
  id: string;
  type: string;
  label: string;
  status: JobStatus;
  display_status: string;
}

export interface JobResult {
  summary?: string;
  files?: string[];
  urls?: string[];
  task_id?: string;
  [key: string]: unknown;
}

export interface Job {
  id: string;
  type: string;
  lane: string;
  status: JobStatus;
  /** Вычисляемый статус: running + cancel_requested → cancelling. */
  display_status: string;
  owner: string;
  target: JobTarget;
  params: Record<string, unknown>;
  priority: number;
  depends_on: string[];
  /** Предпосылки с подписями и статусами (от API). */
  prerequisites?: JobPrerequisite[];
  /** id незавершённых предпосылок: операция ждёт их. */
  blocked_by?: string[];
  attempt: number;
  progress: JobProgress;
  result: JobResult | null;
  error: JobError | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  retention: { expires_at: string | null; keep: boolean };
}

export interface JobTypeManifest {
  key: string;
  lane: string;
  label: string;
  result_kind: string;
  cancellable: boolean;
  resumable: boolean;
  retention_days: number | null;
  params_schema: Record<string, unknown>;
  has_prerequisites: boolean;
}

export interface JobsSummary {
  queued: number;
  running: number;
  done: number;
  error: number;
  cancelled: number;
  interrupted: number;
  active: number;
}

// Атрибут accept для выбора файлов: общий список поддерживаемых расширений.
// «video/*» сам по себе скрывает .avi/.mkv и т.п. в диалоге выбора файла.
export const VIDEO_ACCEPT = [
  ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm",
  ".mts", ".m2ts", ".ts", ".flv", ".wmv",
  ".mpg", ".mpeg", ".3gp", ".3g2", ".ogv", ".ogm", ".asf", ".vob",
  "video/*",
].join(",");
