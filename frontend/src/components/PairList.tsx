import { useState } from "react";
import { deleteWorkspace, uploadWorkspace } from "../api";
import type { VideoPair } from "../types";
import { StatusbarPreview } from "./StatusbarPreview";

interface Props {
  pairs: VideoPair[];
  onSelect: (id: string) => void;
  onChanged: () => void;
}

function fileStem(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}

export function PairList({ pairs, onSelect, onChanged }: Props) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [source, setSource] = useState<File | null>(null);
  const [preview, setPreview] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState("");

  const pickFile = (which: "source" | "preview") => (f: File | null) => {
    if (which === "source") setSource(f);
    else setPreview(f);
    if (f && !nameTouched) setName(fileStem(f.name));
  };

  const reset = () => {
    setShowForm(false);
    setName("");
    setNameTouched(false);
    setSource(null);
    setPreview(null);
    setProgress(null);
    setError("");
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const first = source ?? preview;
    if (!first) {
      setError("Выберите хотя бы один видеофайл");
      return;
    }
    setBusy(true);
    setError("");
    setProgress(0);
    try {
      const created = await uploadWorkspace(
        name.trim() || fileStem(first.name),
        { source: source ?? undefined, preview: preview ?? undefined },
        setProgress,
      );
      reset();
      onChanged();
      onSelect(created.id); // сразу открываем на редактирование
    } catch (err) {
      setError((err as Error).message);
      setProgress(null);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: VideoPair) => {
    const ok = window.confirm(
      `Безвозвратно удалить «${p.id}»?\n` +
        "Будут стёрты видео, фрагменты и результаты экспорта.",
    );
    if (!ok) return;
    try {
      await deleteWorkspace(p.id);
      onChanged();
    } catch (err) {
      window.alert(`Не удалось удалить: ${(err as Error).message}`);
    }
  };

  return (
    <div className="pair-list">
      <div className="pair-list-head">
        <h2>Рабочие пространства</h2>
        <button
          className="add-btn"
          onClick={() => (showForm ? reset() : setShowForm(true))}
        >
          {showForm ? "Отмена" : "＋ Добавить видео"}
        </button>
      </div>

      {showForm && (
        <form className="upload" onSubmit={submit}>
          <label className="upload-role">
            <span>Источник (из него вырезаются фрагменты)</span>
            <input
              type="file"
              accept="video/*"
              onChange={(e) => pickFile("source")(e.target.files?.[0] ?? null)}
            />
          </label>
          <label className="upload-role">
            <span>Превью (что показывается)</span>
            <input
              type="file"
              accept="video/*"
              onChange={(e) => pickFile("preview")(e.target.files?.[0] ?? null)}
            />
          </label>
          <label>
            Имя workspace
            <input
              type="text"
              value={name}
              placeholder="например, vyezd"
              onChange={(e) => {
                setName(e.target.value);
                setNameTouched(true);
              }}
            />
          </label>
          {progress !== null && (
            <div className="upload-progress">
              <div
                className="upload-progress-bar"
                style={{ width: `${Math.round(progress * 100)}%` }}
              />
            </div>
          )}
          {error && <div className="upload-error">{error}</div>}
          <button type="submit" disabled={busy || (!source && !preview)}>
            {progress !== null && busy ? `Загрузка ${Math.round(progress * 100)}%` : "Загрузить"}
          </button>
        </form>
      )}

      <ul>
        {pairs.length === 0 && <li className="empty">Пока нет workspace-ов.</li>}
        {pairs.map((p) => (
          <li key={p.id}>
            <button className="pair-open" onClick={() => onSelect(p.id)}>
              <StatusbarPreview
                className="pair-bg"
                totalFrames={p.total_frames}
                fragments={p.fragments}
                position={p.position}
              />
              <span className="pair-label">
                {p.source_name}
                {p.preview_name && p.preview_name !== p.source_name
                  ? ` → ${p.preview_name}`
                  : ""}
              </span>
              <span className="pair-meta">
                {p.total_frames} кадров · {p.width}×{p.height}
              </span>
            </button>
            <button
              className="pair-delete"
              title="Удалить workspace"
              onClick={() => remove(p)}
            >
              🗑
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}