export interface Fragment {
  id: number;
  video_pair: number;
  start: number;
  end: number;
  comment?: string;
  user: number | null;
  created_at?: string;
  updated_at?: string;
}

export interface VideoPair {
  id: number;
  original_name: string;
  visualization_name: string;
  total_frames: number;
  width: number;
  height: number;
  fps: number;
  chunk_size: number;
  created_at: string;
  original_url: string;
  visualization_url?: string;
}

export interface VideoPairDetail extends VideoPair {
  fragments: { start: number; end: number; comment?: string }[];
}

export interface ExportItem {
  index: number;
  filename: string;
  url: string;
}

export interface ExportResponse {
  files: ExportItem[];
}