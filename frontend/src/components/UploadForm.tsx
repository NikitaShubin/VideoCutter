import { useState } from "react";
import type { FormEvent } from "react";
import { createPair } from "../api";
import type { VideoPairDetail } from "../types";

interface Props {
  onCreated: (pair: VideoPairDetail) => void;
}

export function UploadForm({ onCreated }: Props) {
  const [original, setOriginal] = useState<File | null>(null);
  const [visualization, setVisualization] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!original) {
      setError("Выберите файл оригинала");
      return;
    }
    setUploading(true);
    setError("");
    try {
      const pair = await createPair(original, visualization);
      onCreated(pair);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <form className="upload" onSubmit={onSubmit}>
      <h2>Загрузить видео-пару</h2>
      <label>
        Оригинал (будет вырезаться):
        <input
          type="file"
          accept="video/*"
          onChange={(e) => setOriginal(e.target.files?.[0] ?? null)}
        />
      </label>
      <label>
        Визуализация (пометки НС, не вырезается):
        <input
          type="file"
          accept="video/*"
          onChange={(e) => setVisualization(e.target.files?.[0] ?? null)}
        />
      </label>
      <button type="submit" disabled={uploading}>
        {uploading ? "Загрузка…" : "Загрузить"}
      </button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}