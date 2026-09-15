import { useState } from "react";
import { deleteWorkspace, uploadWorkspace } from "../api";
import type { VideoPair } from "../types";

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
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState("");

  const pickFile = (f: File | null) => {
    setFile(f);
    if (f && !nameTouched) setName(fileStem(f.name));
  };

  const reset = () => {
    setShowForm(false);
    setName("");
    setNameTouched(false);
    setFile(null);
    setProgress(null);
    setError("");
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setError("Выберите видеофайл");
      return;
    }
    setBusy(true);
    setError("");
    setProgress(0);
    try {
      await uploadWorkspace(name.trim() || fileStem(file.name), file, setProgress);
      reset();
      onChanged();
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
          <label>
            Видеофайл
            <input
              type="file"
              accept="video/*"
              onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
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
          <button type="submit" disabled={busy || !file}>
            {progress !== null && busy ? `Загрузка ${Math.round(progress * 100)}%` : "Загрузить"}
          </button>
        </form>
      )}

      <ul>
        {pairs.length === 0 && <li className="empty">Пока нет workspace-ов.</li>}
        {pairs.map((p) => (
          <li key={p.id}>
            <button className="pair-open" onClick={() => onSelect(p.id)}>
              {p.original_name} · {p.visualization_name || ""} · {p.total_frames} кадров · {p.width}×{p.height}
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
