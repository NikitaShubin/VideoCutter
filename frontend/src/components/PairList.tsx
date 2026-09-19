import { useState } from "react";
import {
  assignWorkspaceVideo,
  bumpWorkspaceNonce,
  deleteWorkspace,
  removeWorkspaceVideo,
  renameWorkspace,
  setWorkspaceVideo,
  swapVideos,
  uploadWorkspace,
} from "../api";
import { VIDEO_ACCEPT, type VideoPair } from "../types";
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

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [editProgress, setEditProgress] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editError, setEditError] = useState("");

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
      // Сбрасываем кэш (nonce в frameUrl), чтобы пересозданная под тем же
      // именем задача не подсовывала старые кадры из HTTP-кэша браузера.
      bumpWorkspaceNonce(p.id);
      onChanged();
    } catch (err) {
      window.alert(`Не удалось удалить: ${(err as Error).message}`);
    }
  };

  // ─── Редактирование задачи (inline в списке) ─────────────────────────────

  const openEdit = (p: VideoPair) => {
    setEditingId(p.id);
    setEditName(p.id);
    setEditBusy(false);
    setEditProgress(null);
    setEditError("");
  };

  const withEditOp = async <T,>(op: () => Promise<T>): Promise<T | null> => {
    setEditBusy(true);
    setEditError("");
    setEditProgress(null);
    try {
      return await op();
    } catch (e) {
      setEditError((e as Error).message);
      return null;
    } finally {
      setEditBusy(false);
    }
  };

  const doRename = async (id: string) => {
    const trimmed = editName.trim();
    if (!trimmed) {
      setEditError("Имя не может быть пустым");
      return;
    }
    if (trimmed === id) return;
    await withEditOp(async () => {
      await renameWorkspace(id, trimmed);
      onChanged();
      setEditingId(trimmed);
      setEditName(trimmed);
    });
  };

  const doSwap = async (id: string) => {
    await withEditOp(() => swapVideos(id));
    onChanged();
  };

  const doRemoveRole = async (id: string, role: "source" | "preview") => {
    await withEditOp(() => removeWorkspaceVideo(id, role));
    onChanged();
  };

  const doUploadRole = (id: string, role: "source" | "preview", file: File) => {
    setEditBusy(true);
    setEditError("");
    setEditProgress(0);
    setWorkspaceVideo(id, role, file, undefined, setEditProgress)
      .then(() => {
        onChanged();
        setEditProgress(null);
      })
      .catch((e: Error) => {
        setEditError(e.message);
        setEditProgress(null);
      })
      .finally(() => setEditBusy(false));
  };

  const doAssign = async (id: string, role: "source" | "preview", filename: string) => {
    await withEditOp(() => assignWorkspaceVideo(id, role, filename));
    onChanged();
  };

  // ─── Форма редактирования одной задачи ───────────────────────────────────

  const renderEdit = (p: VideoPair) => (
    <div className="pair-edit">
      <div className="pair-edit-row">
        <span className="pair-edit-label">Имя</span>
        <input
          type="text"
          value={editName}
          onChange={(e) => setEditName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") doRename(p.id); }}
        />
        <button
          className="pair-edit-btn-text"
          onClick={() => doRename(p.id)}
          disabled={editBusy || editName.trim() === p.id}
        >
          Переименовать
        </button>
      </div>

      <div className="role-field">
        <b>Источник</b>
        <span className="role-name">
          {p.source_name === p.preview_name ? "(то же видео)" : p.source_name}
        </span>
        {p.source_name && (
          <>
            <label className="role-action">
              Заменить…
              <input
                type="file"
                hidden
                accept={VIDEO_ACCEPT}
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  if (f) doUploadRole(p.id, "source", f);
                  e.target.value = "";
                }}
              />
            </label>
            {p.source_name !== p.preview_name && (
              <button
                className="role-action"
                onClick={() => doRemoveRole(p.id, "source")}
                disabled={editBusy}
              >
                Убрать
              </button>
            )}
          </>
        )}
      </div>

      <div className="role-field">
        <b>Превью</b>
        <span className="role-name">
          {p.preview_name === p.source_name ? "(то же видео)" : p.preview_name}
        </span>
        {p.preview_name && (
          <>
            <label className="role-action">
              Заменить…
              <input
                type="file"
                hidden
                accept={VIDEO_ACCEPT}
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  if (f) doUploadRole(p.id, "preview", f);
                  e.target.value = "";
                }}
              />
            </label>
            {p.preview_name !== p.source_name && (
              <button
                className="role-action"
                onClick={() => doRemoveRole(p.id, "preview")}
                disabled={editBusy}
              >
                Убрать
              </button>
            )}
          </>
        )}
      </div>

      {p.unassigned_name && (
        <div className="role-field unassigned">
          <b>Без роли</b>
          <span className="role-name">{p.unassigned_name}</span>
          <button
            className="role-action"
            onClick={() => doAssign(p.id, "source", p.unassigned_name!)}
            disabled={editBusy}
          >
            → источник
          </button>
          <button
            className="role-action"
            onClick={() => doAssign(p.id, "preview", p.unassigned_name!)}
            disabled={editBusy}
          >
            → превью
          </button>
        </div>
      )}

      {p.source_name !== p.preview_name && (
        <div className="pair-edit-row" style={{ marginTop: 4 }}>
          <button
            className="role-action"
            onClick={() => doSwap(p.id)}
            disabled={editBusy}
          >
            ⇅ Поменять местами
          </button>
        </div>
      )}

      {editProgress !== null && (
        <div className="pair-edit-progress">
          <div className="pair-edit-progress-bar" style={{ width: `${Math.round(editProgress * 100)}%` }} />
        </div>
      )}
      {editError && <div className="pair-edit-error">{editError}</div>}

      <div className="pair-edit-row">
        <button
          className="pair-edit-btn-text"
          onClick={() => { setEditingId(null); onChanged(); }}
        >
          Готово
        </button>
      </div>
    </div>
  );

  // ─── Render ──────────────────────────────────────────────────────────────

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
              accept={VIDEO_ACCEPT}
              onChange={(e) => pickFile("source")(e.target.files?.[0] ?? null)}
            />
          </label>
          <label className="upload-role">
            <span>Превью (что показывается)</span>
            <input
              type="file"
              accept={VIDEO_ACCEPT}
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
          <li key={p.id} className={editingId === p.id ? "editing" : undefined}>
            <div className="pair-row">
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
                {p.pair_warning && (
                  <span className="pair-warn" title={p.pair_warning}>
                    ⚠ {p.pair_warning}
                  </span>
                )}
              </button>
              <div className="pair-actions">
                <button
                  className="pair-edit-btn"
                  title="Изменить"
                  onClick={() => openEdit(p)}
                >
                  ✎
                </button>
                <button
                  className="pair-delete"
                  title="Удалить workspace"
                  onClick={() => remove(p)}
                >
                  🗑
                </button>
              </div>
            </div>
            {editingId === p.id && renderEdit(p)}
          </li>
        ))}
      </ul>
    </div>
  );
}
