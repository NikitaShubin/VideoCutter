export interface Fragment {
  start: number;
  end: number;
  comment?: string;
}

export interface VideoPair {
  id: string;
  original_name: string;
  visualization_name: string;
  total_frames: number;
  width: number;
  height: number;
  fps: number;
}

export interface VideoPairDetail extends VideoPair {
  fragments: Fragment[];
}

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
