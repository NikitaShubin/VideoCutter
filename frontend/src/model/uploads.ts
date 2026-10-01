import { useCallback, useEffect, useRef, useState } from "react";
import {
  getUploadStatus,
  uploadWorkspace,
  type WorkspaceUploadFiles,
} from "../api";

export interface UploadJob {
  /** Client-ключ строки (он же upload_id для статуса приёма). */
  key: string;
  /** Показываемое имя (оптимистично — сразу новое после rename). */
  name: string;
  uploadId: string;
  /** Server id после 202 (null — POST ещё летит, записи нет). */
  serverId: string | null;
  /** Новое имя, ждущее готовности записи (PATCH только по готовой). */
  pendingRename: string | null;
  /** Доля отправки 0..1 (XHR upload.onprogress). */
  progress: number;
  /** Байты, принятые сервером (опрос статуса), null — ещё не спрашивали. */
  serverReceived: number | null;
  serverTotal: number | null;
  error: string | null;
}

export function newUploadId(): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID().replace(/-/g, "");
    }
  } catch {
    /* fallthrough */
  }
  const h = () =>
    Math.floor(Math.random() * 0xffffffff)
      .toString(16)
      .padStart(8, "0");
  return h() + h() + h() + h();
}

/** Доля для бара строки: отправка → приём сервером (оба 0..1). */
export function uploadFraction(u: UploadJob): number {
  if (u.progress < 1) return u.progress;
  if (u.serverTotal) {
    return Math.min(1, (u.serverReceived ?? 0) / Math.max(1, u.serverTotal));
  }
  return 1;
}

export function uploadLabel(u: UploadJob): string {
  if (u.error) return u.error;
  if (u.progress < 1) return `Загрузка ${Math.round(u.progress * 100)}%`;
  if (u.serverTotal && (u.serverReceived ?? 0) < u.serverTotal) {
    return `Приём сервером ${Math.round(uploadFraction(u) * 100)}%`;
  }
  return "Готово…";
}

/** Заливки живут в App (переживают смену экранов): форма только стартует,
 * строки списка показывают прогресс, ведро отменяет. Автооткрытия нет
 * осознанно: пользователь может работать над другой задачей, угон
 * экрана запрещён.
 */
export function useUploadManager(onChanged: () => void) {
  const [uploads, setUploads] = useState<UploadJob[]>([]);
  const controllers = useRef(new Map<string, AbortController>());
  const uploadsRef = useRef(uploads);
  uploadsRef.current = uploads;

  // Опрос приёма сервером — только у полностью отправленных без
  // serverId (после 202 трансфер заведомо цел, спрашивать нечего).
  const awaiting = uploads
    .filter((u) => !u.error && u.progress >= 1 && !u.serverId)
    .map((u) => u.key)
    .join(",");
  useEffect(() => {
    if (!awaiting) return;
    const keys = awaiting.split(",");
    const id = window.setInterval(() => {
      keys.forEach((key) => {
        const job = uploadsRef.current.find((p) => p.key === key);
        if (!job) return;
        getUploadStatus(job.uploadId)
          .then((st) => {
            if (!st) return;
            setUploads((prev) =>
              prev.map((p) =>
                p.key === key
                  ? { ...p, serverReceived: st.received, serverTotal: st.total }
                  : p,
              ),
            );
          })
          .catch(() => {});
      });
    }, 1000);
    return () => window.clearInterval(id);
  }, [awaiting]);

  // Reload вкладки убивает XHR и job'ы (память): предупреждаем, пока
  // есть активные заливки. После 202 бояться нечего — запись серверная.
  const hasActive = uploads.some((u) => !u.error);
  useEffect(() => {
    if (!hasActive) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [hasActive]);

  const startUpload = useCallback(
    (name: string, files: WorkspaceUploadFiles) => {
      const id = newUploadId();
      const controller = new AbortController();
      controllers.current.set(id, controller);
      setUploads((prev) => [
        ...prev,
        {
          key: id,
          name,
          uploadId: id,
          serverId: null,
          pendingRename: null,
          progress: 0,
          serverReceived: null,
          serverTotal: null,
          error: null,
        },
      ]);
      uploadWorkspace(
        name,
        files,
        (f) => {
          setUploads((prev) =>
            prev.map((u) => (u.key === id ? { ...u, progress: f } : u)),
          );
        },
        { signal: controller.signal, uploadId: id },
      )
        .then((entry) => {
          controllers.current.delete(id);
          // 202: запись есть (indexing), job живёт до её готовности —
          // строка морфирует фазами без дублей и без угона экрана.
          setUploads((prev) =>
            prev.map((u) => (u.key === id ? { ...u, serverId: entry.id } : u)),
          );
          onChanged(); // серверная запись подхватится списком
        })
        .catch((e: Error) => {
          controllers.current.delete(id);
          if (e?.name === "AbortError") {
            setUploads((prev) => prev.filter((u) => u.key !== id));
            onChanged();
            return;
          }
          setUploads((prev) =>
            prev.map((u) => (u.key === id ? { ...u, error: e.message } : u)),
          );
        });
    },
    [onChanged],
  );

  const renameUpload = useCallback((key: string, name: string) => {
    // Оптимистично сразу; применится PATCH по готовности записи
    // (эффект в PairList) — rename гонки с валидацией исключены.
    setUploads((prev) =>
      prev.map((u) =>
        u.key === key ? { ...u, name, pendingRename: name } : u,
      ),
    );
  }, []);

  const updateJob = useCallback(
    (key: string, patch: Partial<UploadJob>) => {
      setUploads((prev) =>
        prev.map((u) => (u.key === key ? { ...u, ...patch } : u)),
      );
    },
    [],
  );

  const removeJob = useCallback((key: string) => {
    controllers.current.delete(key);
    setUploads((prev) => prev.filter((u) => u.key !== key));
  }, []);

  const cancelUpload = useCallback(
    (key: string) => {
      // Abort роняет XHR (catch отметит AbortError и уберёт строку);
      // страховка на случай гонки — убрать сразу. Недозалитое чистит
      // сервер (upload_interrupted + sweep), созданное — ведром задачи.
      controllers.current.get(key)?.abort();
      controllers.current.delete(key);
      setUploads((prev) => prev.filter((u) => u.key !== key));
      onChanged();
    },
    [onChanged],
  );

  const dismissUpload = useCallback((key: string) => {
    setUploads((prev) => prev.filter((u) => u.key !== key));
  }, []);

  return {
    uploads,
    startUpload,
    cancelUpload,
    dismissUpload,
    renameUpload,
    updateJob,
    removeJob,
  };
}
