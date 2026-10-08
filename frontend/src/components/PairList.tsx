import { useCallback, useEffect, useRef, useState } from "react";
import {
  annotationsUrl,
  attachTask,
  assignWorkspaceVideo,
  bumpWorkspaceNonce,
  createProject,
  deleteProject,
  deleteWorkspace,
  detachTask,
  downloadFile,
  importAnnotations,
  importBackup,
  listProjects,
  projectBackupUrl,
  removeWorkspaceVideo,
  renameProject,
  renameWorkspace,
  setWorkspaceVideo,
  swapVideos,
  taskBackupUrl,
} from "../api";
import { VIDEO_ACCEPT, type Project, type VideoPair } from "../types";
import {
  uploadLabel,
  type UploadJob,
} from "../model/uploads";
import { StatusbarPreview } from "./StatusbarPreview";

interface Props {
  pairs: VideoPair[] | null;
  onSelect: (id: string) => void;
  onChanged: () => void;
  uploads: UploadJob[];
  onStartUpload: (name: string, files: { source?: File; preview?: File }) => void;
  onCancelUpload: (key: string) => void;
  onDismissUpload: (key: string) => void;
  onRenameUpload: (key: string, name: string) => void;
  onUpdateJob: (key: string, patch: Partial<UploadJob>) => void;
  onRemoveJob: (key: string) => void;
}

function fileStem(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}

type SortMode = "updated" | "created" | "opened" | "name";

type Tab = "tasks" | "projects";

const SORT_KEY = "vc-sort";
const TAB_KEY = "vc-tab";

/** Режим фильтра по проекту (localStorage): все / без проекта / id проекта. */
const PROJ_KEY = "vc-project";
const PROJ_ALL = "__all__";
const PROJ_NONE = "__none__";

function readProjectFilter(): string {
  try {
    return window.localStorage.getItem(PROJ_KEY) ?? PROJ_ALL;
  } catch {
    return PROJ_ALL;
  }
}

function readSortMode(): SortMode {
  try {
    const v = window.localStorage.getItem(SORT_KEY);
    if (v === "created" || v === "opened" || v === "name" || v === "updated") return v;
  } catch {
    /* ignore */
  }
  return "updated";
}

function readTab(): Tab {
  try {
    return window.localStorage.getItem(TAB_KEY) === "projects"
      ? "projects"
      : "tasks";
  } catch {
    return "tasks";
  }
}

function timeKey(s: string | null): number {
  if (!s) return Number.NEGATIVE_INFINITY;
  const t = Date.parse(s);
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

function sortPairs(pairs: VideoPair[], mode: SortMode): VideoPair[] {
  const arr = [...pairs];
  switch (mode) {
    case "name":
      return arr.sort((a, b) => a.id.localeCompare(b.id, "ru"));
    case "created":
      return arr.sort((a, b) => timeKey(b.created_at) - timeKey(a.created_at));
    case "opened":
      return arr.sort((a, b) => timeKey(b.last_opened_at) - timeKey(a.last_opened_at));
    case "updated":
    default:
      return arr.sort((a, b) => b.updated_at - a.updated_at);
  }
}

export function PairList({
  pairs,
  onSelect,
  onChanged,
  uploads,
  onStartUpload,
  onCancelUpload,
  onDismissUpload,
  onRenameUpload,
  onUpdateJob,
  onRemoveJob,
}: Props) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [source, setSource] = useState<File | null>(null);
  const [preview, setPreview] = useState<File | null>(null);
  const [formError, setFormError] = useState("");

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [editProgress, setEditProgress] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editProject, setEditProject] = useState("");
  const [editError, setEditError] = useState("");

  const firstFile = source ?? preview;

  const [sortMode, setSortMode] = useState<SortMode>(readSortMode);
  const visible = pairs === null ? null : sortPairs(pairs, sortMode);

  // ─── Вкладки (как в CVAT): задачи и проекты — отдельные экраны ────────
  const [tab, setTab] = useState<Tab>(readTab);

  const pickTab = (t: Tab) => {
    setTab(t);
    try {
      window.localStorage.setItem(TAB_KEY, t);
    } catch {
      /* ignore */
    }
  };

  // ─── Проекты: реестр, фильтр списка, CRUD ────────────────────────────────
  const [projFilter, setProjFilter] = useState<string>(readProjectFilter);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [projError, setProjError] = useState("");
  // Восстановление из бэкапа: XHR-заливка с прогрессом (default rename —
  // совпадающие имена получают суффикс _1, ничего не затирается).
  const [projRestoreProgress, setProjRestoreProgress] = useState<number | null>(null);
  // Скачивание бэкапа проекта: id строки со спиннером (архив собирается
  // синхронно — без отклика кнопка выглядит мёртвой).
  const [dlKey, setDlKey] = useState<string | null>(null);

  const loadProjects = useCallback(() => {
    listProjects()
      .then((items) => {
        setProjects(items);
        setProjError("");
      })
      .catch((e) => {
        // Реестр не поднялся — список задач не должен упасть.
        setProjects((prev) => prev ?? []);
        setProjError((e as Error).message);
      });
  }, []);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  const pickProjectFilter = (v: string) => {
    setProjFilter(v);
    try {
      window.localStorage.setItem(PROJ_KEY, v);
    } catch {
      /* ignore */
    }
  };

  // Открыть проект: вкладка задач, отфильтрованная по нему (drill-down).
  const openProject = (id: string) => {
    pickProjectFilter(id);
    pickTab("tasks");
  };

  const projectName = (pid: string | null): string | null => {
    if (pid == null) return null;
    return (projects ?? []).find((p) => p.id === pid)?.name ?? pid;
  };

  const [query, setQuery] = useState("");

  const listed =
    visible === null
      ? null
      : visible.filter((p) => {
          const byProject =
            projFilter === PROJ_ALL
              ? true
              : projFilter === PROJ_NONE
                ? p.project_id == null
                : p.project_id === projFilter;
          if (!byProject) return false;
          const q = query.trim().toLowerCase();
          return !q || p.id.toLowerCase().includes(q);
        });

  // Пока есть индексирующиеся задачи или бегущий экспорт — опрашиваем
  // список (фон сервера). Опрос прекращается, когда всё тихо.
  useEffect(() => {
    if (
      !pairs ||
      !pairs.some(
        (p) =>
          p.indexing ||
          p.export?.state === "running" ||
          p.export?.state === "cancelling",
      )
    )
      return;
    const id = window.setInterval(onChanged, 2000);
    return () => window.clearInterval(id);
  }, [pairs, onChanged]);

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
    setFormError("");
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const first = source ?? preview;
    if (!first) {
      setFormError("Выберите хотя бы один видеофайл");
      return;
    }
    // Форма только стартует заливку и закрывается: прогресс живёт
    // строкой списка (менеджер в App переживает смену экранов),
    // отмена — ведром на строке.
    onStartUpload(name.trim() || fileStem(first.name), {
      source: source ?? undefined,
      preview: preview ?? undefined,
    });
    reset();
  };

  // Синхронизация заливок с серверными записями. Job живёт до
  // готовности записи (без автооткрытия — угон экрана запрещён):
  // готова без rename — снять job (встаёт серверная строка);
  // broken — снять job (ошибку показывает серверная broken-строка);
  // pendingRename — PATCH по готовой записи (rename только вне
  // валидации/индекса, иначе гонка путей). PATCH идёт один за раз.
  const renamingRef = useRef(new Set<string>());
  useEffect(() => {
    if (!pairs) return;
    uploads.forEach((u) => {
      if (!u.serverId || renamingRef.current.has(u.key)) return;
      const entry = pairs.find((p) => p.id === u.serverId);
      if (!entry) return;
      if (entry.broken) {
        onRemoveJob(u.key);
        return;
      }
      if (!entry.indexing) {
        if (u.pendingRename) {
          renamingRef.current.add(u.key);
          renameWorkspace(entry.id, u.pendingRename)
            .then((updated) => {
              renamingRef.current.delete(u.key);
              onUpdateJob(u.key, {
                serverId: updated.id,
                pendingRename: null,
                name: updated.id,
              });
              onChanged();
            })
            .catch((e: Error) => {
              renamingRef.current.delete(u.key);
              onUpdateJob(u.key, { pendingRename: null, error: e.message });
            });
        } else {
          onRemoveJob(u.key);
        }
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairs, uploads]);

  const renameUpload = (u: UploadJob) => {
    const v = window.prompt("Новое имя задачи", u.name);
    if (v === null) return;
    const t = v.trim();
    if (!t || t === u.name) return;
    onRenameUpload(u.key, t);
  };

  // Серверный дубликат подавляется, пока жив job (иначе две строки
  // об одном: клиентская морфирует фазами, серверная встанет после).
  const shownPairs = (listed ?? []).filter(
    (p) => !uploads.some((u) => u.serverId !== null && u.serverId === p.id),
  );

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

  // ─── Управление проектами (панель в шапке списка) ────────────────────────

  const projOp = async (op: () => Promise<unknown>) => {
    try {
      await op();
      setProjError("");
      loadProjects();
      return true;
    } catch (e) {
      setProjError((e as Error).message);
      return false;
    }
  };

  const addProject = async () => {
    const pname = newProjectName.trim();
    if (!pname) return;
    if (await projOp(() => createProject(pname))) setNewProjectName("");
  };

  const editProjectName = async (p: Project) => {
    const v = window.prompt("Новое имя проекта", p.name);
    if (v === null) return;
    const t = v.trim();
    if (!t || t === p.name) return;
    await projOp(() => renameProject(p.id, t));
  };

  const removeProject = async (p: Project, mode: "keep" | "cascade") => {
    const ok =
      mode === "keep"
        ? window.confirm(
            `Удалить проект «${p.name}»?\n` +
              `Его задачи (${p.task_count}) останутся без проекта.`,
          )
        : window.confirm(
            `⚠ Удалить проект «${p.name}» ВМЕСТЕ с ` +
              `${p.task_count} задачами?\n` +
              "Будут стёрты видео, фрагменты и результаты экспорта.",
          );
    if (!ok) return;
    if (await projOp(() => deleteProject(p.id, mode))) {
      if (projFilter === p.id) pickProjectFilter(PROJ_ALL);
      onChanged();
    }
  };

  // Перевеска из формы задачи: оптимистично двигаем селект, при сбое
  // откатываем и показываем ошибку формы.
  const changeProject = async (
    p: VideoPair,
    prev: string,
    next: string,
  ) => {
    if (prev === next) return;
    try {
      if (next) await attachTask(next, p.id);
      else if (prev) await detachTask(prev, p.id);
      setEditError("");
      loadProjects();
      onChanged();
    } catch (e) {
      setEditProject(prev);
      setEditError((e as Error).message);
    }
  };

  // ─── Редактирование задачи (inline в списке) ─────────────────────────────

  const openEdit = (p: VideoPair) => {
    setEditingId(p.id);
    setEditName(p.id);
    setEditProject(p.project_id ?? "");
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

  // Импорт разметки из архива annotations (только fragments.tsv; видео
  // задачи не трогается). Прогресс — в тот же бар формы, ошибка — туда же.
  const doImportAnnotations = (id: string, file: File) => {
    setEditBusy(true);
    setEditError("");
    setEditProgress(0);
    importAnnotations(id, file, { onProgress: setEditProgress })
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

  // Восстановление задачи/проекта из бэкапа (POST /backups/import, 202 —
  // файлы записаны, индекс догоняет фоном; список опрашивается сам).
  const doRestoreBackup = (file: File) => {
    setProjError("");
    setProjRestoreProgress(0);
    importBackup(file, { onProgress: setProjRestoreProgress })
      .then(() => {
        setProjRestoreProgress(null);
        loadProjects();
        onChanged();
      })
      .catch((e: Error) => {
        setProjError(e.message);
        setProjRestoreProgress(null);
      });
  };

  // Скачивание бэкапа проекта с откликом (сборка — на сервере, синхронно).
  const doDownloadProject = (p: Project) => {
    if (dlKey !== null) return;
    setDlKey(p.id);
    setProjError("");
    downloadFile(projectBackupUrl(p.id), `${p.id}-backup.zip`)
      .catch((e: Error) => setProjError(e.message))
      .finally(() => setDlKey(null));
  };

  // Скачивание бэкапа/разметки задачи: прогресс — в бар формы.
  const doDownloadTask = (id: string, kind: "backup" | "annotations") => {
    if (editBusy) return;
    setEditBusy(true);
    setEditError("");
    setEditProgress(0);
    const url = kind === "backup" ? taskBackupUrl(id) : annotationsUrl(id);
    downloadFile(url, `${id}-${kind}.zip`, (f) => setEditProgress(f ?? 0))
      .catch((e: Error) => setEditError(e.message))
      .finally(() => {
        setEditBusy(false);
        setEditProgress(null);
      });
  };

  // ─── Форма редактирования одной задачи ───────────────────────────────────

  const renderEdit = (p: VideoPair) => {
    // Переименование во время индексации/валидации — гонка абсолютных
    // путей (движок и фоновая проверка держат старый каталог): кнопка
    // блокируется до готовности, как и открытие задачи.
    const live = pairs?.find((x) => x.id === p.id);
    const renameLocked = live?.indexing ?? p.indexing;
    return (
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
          disabled={editBusy || editName.trim() === p.id || renameLocked}
          title={renameLocked ? "Дождитесь готовности задачи" : "Переименовать"}
        >
          Переименовать
        </button>
      </div>

      <div className="pair-edit-row">
        <span className="pair-edit-label">Проект</span>
        <select
          className="pair-edit-select"
          value={editProject}
          disabled={editBusy}
          onChange={(e) => {
            const prev = editProject;
            const next = e.target.value;
            setEditProject(next);
            void changeProject(p, prev, next);
          }}
        >
          <option value="">— без проекта —</option>
          {(projects ?? []).map((pr) => (
            <option key={pr.id} value={pr.id}>
              {pr.name}
            </option>
          ))}
        </select>
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
      {editProgress !== null && editBusy && editProgress >= 1 && (
        <div className="pair-edit-verifying">Проверка видео…</div>
      )}
      {editError && <div className="pair-edit-error">{editError}</div>}

      <div className="pair-edit-row">
        <span className="pair-edit-label">Бэкап</span>
        <button
          className="role-action"
          disabled={editBusy}
          onClick={() => doDownloadTask(p.id, "backup")}
          title="Скачать полный бэкап задачи (видео + разметка + паспорт)"
        >
          ⤓ Бэкап
        </button>
        <button
          className="role-action"
          disabled={editBusy}
          onClick={() => doDownloadTask(p.id, "annotations")}
          title="Скачать разметку (fragments.tsv + манифест)"
        >
          ⤓ Разметка
        </button>
        <label
          className="role-action"
          title="Применить архив разметки к этой задаче (видео не трогается)"
        >
          Импорт разметки…
          <input
            type="file"
            hidden
            accept=".zip,application/zip"
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              if (f) doImportAnnotations(p.id, f);
              e.target.value = "";
            }}
          />
        </label>
      </div>

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
  };

  // ─── Render ──────────────────────────────────────────────────────────────

  // ─── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="pair-list">
      <div className="pair-list-head">
        <div className="title-row">
          <div className="tabs" role="tablist" aria-label="Разделы">
            <button
              role="tab"
              aria-selected={tab === "tasks"}
              className={tab === "tasks" ? "tab active" : "tab"}
              onClick={() => pickTab("tasks")}
            >
              Задачи{" "}
              <span className="tab-count">
                {listed !== null ? listed.length : "…"}
              </span>
            </button>
            <button
              role="tab"
              aria-selected={tab === "projects"}
              className={tab === "projects" ? "tab active" : "tab"}
              onClick={() => pickTab("projects")}
            >
              Проекты{" "}
              <span className="tab-count">{projects !== null ? projects.length : "…"}</span>
            </button>
          </div>
          <div className="title-actions">
            {tab === "tasks" && (
              <button
                className="btn primary"
                onClick={() => (showForm ? reset() : setShowForm(true))}
              >
                {showForm ? "Отмена" : "＋ Добавить видео"}
              </button>
            )}
          </div>
        </div>
        {tab === "tasks" && (
          <div className="toolbar-row">
            <input
              className="search"
              type="search"
              value={query}
              placeholder="Поиск по имени задачи…"
              aria-label="Поиск по имени задачи"
              onChange={(e) => setQuery(e.target.value)}
            />
            <label className="field">
              Сортировка{" "}
              <select
                value={sortMode}
                onChange={(e) => {
                  const v = e.target.value as SortMode;
                  setSortMode(v);
                  try {
                    window.localStorage.setItem(SORT_KEY, v);
                  } catch {
                    /* ignore */
                  }
                }}
              >
                <option value="updated">Недавние</option>
                <option value="created">Новые</option>
                <option value="opened">Открытые</option>
                <option value="name">По имени</option>
              </select>
            </label>
            <label className="field">
              Проект{" "}
              <select
                value={projFilter}
                onChange={(e) => pickProjectFilter(e.target.value)}
              >
                <option value={PROJ_ALL}>Все проекты</option>
                <option value={PROJ_NONE}>Без проекта</option>
                {(projects ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
      </div>

      {tab === "projects" && (
        <div className="project-panel">
          <div className="project-create">
            <input
              type="text"
              value={newProjectName}
              placeholder="Название нового проекта"
              onChange={(e) => setNewProjectName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addProject();
              }}
            />
            <button
              className="btn"
              onClick={() => void addProject()}
              disabled={!newProjectName.trim()}
            >
              Создать проект
            </button>
            <label
              className="btn"
              title="Восстановить задачу/проект из zip-бэкапа (совпадающие имена получат суффикс _1)"
            >
              Восстановить из бэкапа…
              <input
                type="file"
                hidden
                accept=".zip,application/zip"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  if (f) doRestoreBackup(f);
                  e.target.value = "";
                }}
              />
            </label>
          </div>
          {projRestoreProgress !== null && (
            <div className="pair-edit-progress">
              <div
                className="pair-edit-progress-bar"
                style={{ width: `${Math.round(projRestoreProgress * 100)}%` }}
              />
            </div>
          )}
          {projError && <div className="pair-edit-error">{projError}</div>}
          <ul className="project-rows">
            {(projects ?? []).map((p) => (
              <li key={p.id}>
                <button
                  className="project-open"
                  title="Показать задачи проекта"
                  onClick={() => openProject(p.id)}
                >
                  <span className="project-name">{p.name}</span>
                  <span className="project-meta">
                    {p.task_count} задач →
                  </span>
                </button>
                <div className="pair-actions">
                  <button
                    className="pair-edit-btn"
                    title="Скачать бэкап проекта"
                    disabled={dlKey !== null}
                    onClick={() => doDownloadProject(p)}
                  >
                    {dlKey === p.id ? <span className="btn-spin" /> : "⤓"}
                  </button>
                  <button
                    className="pair-edit-btn"
                    title="Переименовать проект"
                    onClick={() => void editProjectName(p)}
                  >
                    ✎
                  </button>
                  <button
                    className="pair-edit-btn"
                    title="Удалить проект (задачи останутся без проекта)"
                    onClick={() => void removeProject(p, "keep")}
                  >
                    ✕
                  </button>
                  <button
                    className="pair-delete"
                    title="Удалить проект ВМЕСТЕ с задачами и видео"
                    onClick={() => void removeProject(p, "cascade")}
                  >
                    🗑
                  </button>
                </div>
              </li>
            ))}
            {projects !== null && projects.length === 0 && (
              <li className="empty">Проектов пока нет.</li>
            )}
          </ul>
          <div className="project-hint">
            Задачи перевешиваются в их форме редактирования (✎ → «Проект»).
          </div>
        </div>
      )}

      {tab === "tasks" && (
        <>
          {showForm && (
            <form className="upload" onSubmit={submit}>
              <h3>Новое видео</h3>
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
          <div className="name-preview">
            Задача будет называться: «
            {name.trim() || (firstFile ? fileStem(firstFile.name) : "—")}»
          </div>
          {formError && <div className="upload-error">{formError}</div>}
          <button type="submit" disabled={!source && !preview}>
            Загрузить
          </button>
        </form>
      )}

      <ul>
        {pairs === null && <li className="loading">Загрузка списка…</li>}
        {uploads.map((u) => {
          const entry =
            u.serverId && pairs
              ? pairs.find((p) => p.id === u.serverId) ?? null
              : null;
          const meta = u.error
            ? `⚠ ${u.error}`
            : entry && entry.indexing
              ? entry.indexing_progress != null
                ? `Индексируется… ${Math.round(entry.indexing_progress * 100)}%`
                : "Индексируется…"
              : uploadLabel(u);
          // Канва — слои фаз: фон = цвет завершённой предыдущей фазы
          // (или тёмный), заливка = цвет текущей. Преемственность видна
          // буквально: каждый этап ложится поверх предыдущего.
          // upload #0077ff → receipt #00bebe → indexing #8b5cf6.
          const layers =
            entry && entry.indexing
              ? {
                base: "#00bebe",
                fill: "#8b5cf6",
                frac: entry.indexing_progress ?? 0,
              }
              : u.progress < 1
                ? { base: "#14141f", fill: "#0077ff", frac: u.progress }
                : {
                  base: "#0077ff",
                  fill: "#00bebe",
                  frac: u.serverTotal
                    ? Math.min(
                      1,
                      (u.serverReceived ?? 0) / Math.max(1, u.serverTotal),
                    )
                    : 0,
                };
          return (
            <li key={u.key}>
              <div className="pair-row">
                <div className="pair-open" aria-disabled="true">
                  <StatusbarPreview
                    className="pair-bg"
                    progress={layers}
                  />
                  <span className="pair-label">{u.name}</span>
                  <span className="pair-meta">{meta}</span>
                </div>
                <div className="pair-actions">
                  {u.error ? (
                    <button
                      className="pair-edit-btn"
                      title="Убрать из списка"
                      onClick={() => onDismissUpload(u.key)}
                    >
                      ✕
                    </button>
                  ) : (
                    <>
                      <button
                        className="pair-edit-btn"
                        title="Переименовать задачу"
                        onClick={() => renameUpload(u)}
                      >
                        ✎
                      </button>
                      <button
                        className="pair-delete"
                        title="Отменить создание задачи"
                        onClick={() => onCancelUpload(u.key)}
                      >
                        🗑
                      </button>
                    </>
                  )}
                </div>
              </div>
            </li>
          );
        })}
        {listed !== null &&
          listed.length === 0 &&
          uploads.length === 0 && (
            <li className="empty">
              {query.trim()
                ? `По запросу «${query.trim()}» ничего не найдено.`
                : visible !== null && visible.length > 0
                  ? "В выбранном проекте нет задач."
                  : "Пока нет задач."}
            </li>
          )}
        {shownPairs.map((p) =>
          p.broken ? (
            <li key={p.id} className="broken">
              <div className="pair-row">
                <span className="pair-label">{p.id}</span>
                <span className="pair-meta">
                  ⚠ {p.error || "Не удалось прочитать задачу"}
                </span>
                <div className="pair-actions">
                  <button
                    className="pair-delete"
                    title="Удалить workspace"
                    onClick={() => remove(p)}
                  >
                    🗑
                  </button>
                </div>
              </div>
            </li>
          ) : (
          <li key={p.id} className={editingId === p.id ? "editing" : undefined}>
            <div className="pair-row">
              <button
                className="pair-open"
                onClick={() => onSelect(p.id)}
                disabled={p.indexing}
                title={p.indexing ? "Индексируется — подождите готовности" : `Открыть ${p.id}`}
              >
                <StatusbarPreview
                  className="pair-bg"
                  totalFrames={p.total_frames}
                  fragments={p.fragments}
                  position={p.position}
                  scheme={p.indexing ? "indexing" : undefined}
                  progress={p.indexing ? {
                    base: "#14141f",
                    fill: "#8b5cf6",
                    frac: p.indexing_progress ?? 0,
                  } : undefined}
                />
                <span className="pair-label">{p.id}</span>
                <span className="pair-meta">
                  {p.source_name}
                  {p.preview_name && p.preview_name !== p.source_name
                    ? ` → ${p.preview_name}`
                    : ""}
                  {projectName(p.project_id) !== null &&
                    ` · ⧉ ${projectName(p.project_id)}`}
                  {p.indexing
                    ? (p.indexing_progress != null
                      ? ` · Индексируется… ${Math.round(p.indexing_progress * 100)}%`
                      : " · ⏳ Индексируется…")
                    : ` · ${p.total_frames} кадров · ${p.width}×${p.height}`}
                </span>
                {p.pair_warning && (
                  <span className="pair-warn" title={p.pair_warning}>
                    ⚠ {p.pair_warning}
                  </span>
                )}
                {p.export?.state === "running" && (
                  <span className="pair-warn" title="Экспорт выполняется">
                    ⏳ экспорт {p.export.index}/{p.export.total}
                  </span>
                )}
                {p.export?.state === "cancelling" && (
                  <span className="pair-warn">⏳ отмена экспорта…</span>
                )}
                {p.export?.state === "done" && (
                  <span
                    className="pair-warn"
                    title="Нарезки готовы — откройте задачу"
                  >
                    ✓ нарезки готовы
                  </span>
                )}
                {p.export?.state === "error" && (
                  <span
                    className="pair-warn"
                    title={p.export.error || "Ошибка экспорта"}
                  >
                    ⚠ ошибка экспорта
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
          )
        )}
      </ul>
        </>
      )}
    </div>
  );
}
