export interface Fragment {
  start: number;
  end: number;
  comment?: string;
}

export interface VideoPair {
  id: string;
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
}

export interface VideoPairDetail extends VideoPair {}

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
  /** Сигнатура экспортированных границ [[start,end],...] — для пропуска повтора. */
  sig?: string;
  error?: string;
}

// Атрибут accept для выбора файлов: общий список поддерживаемых расширений.
// «video/*» сам по себе скрывает .avi/.mkv и т.п. в диалоге выбора файла.
export const VIDEO_ACCEPT = [
  ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm",
  ".mts", ".m2ts", ".ts", ".flv", ".wmv",
  ".mpg", ".mpeg", ".3gp", ".3g2", ".ogv", ".ogm", ".asf", ".vob",
  "video/*",
].join(",");
