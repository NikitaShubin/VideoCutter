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
  fragments: Fragment[];
  position: number;
  updated_at: number;
}

export interface VideoPairDetail extends VideoPair {}

export interface ExportItem {
  index: number;
  filename: string;
  url: string;
}

export interface ExportStatus {
  state: "idle" | "running" | "done" | "error";
  index?: number;
  total?: number;
  files?: ExportItem[];
  error?: string;
}
