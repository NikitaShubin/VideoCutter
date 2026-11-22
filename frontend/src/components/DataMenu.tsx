import type { Job } from "../types";
import { JobAction, JobFileAction } from "./JobActions";

interface Props {
  pairId: string;
  disabled?: boolean;
  onAnnotationsImported?: (job: Job) => void;
  onError?: (message: string) => void;
}

/** Данные: разметка и бэкапы — фоновые операции с прогрессом и отменой. */
export function DataMenu({
  pairId,
  disabled,
  onAnnotationsImported,
  onError,
}: Props) {
  return (
    <div className="menu-stack">
      <JobAction
        buttonClassName="menu-action"
        label="Скачать разметку"
        shortLabel="разметку"
        title="Скачать fragments.tsv + манифест"
        disabled={disabled}
        request={() => ({
          type: "annotations.export",
          target: { kind: "task", id: pairId },
        })}
        downloadName={`${pairId}-annotations.zip`}
        onError={onError}
      />
      <JobFileAction
        buttonClassName="menu-action"
        label="Импортировать разметку"
        shortLabel="импорт"
        title="Применить архив к задаче; видео не меняется"
        disabled={disabled}
        request={(uploadId) => ({
          type: "annotations.import",
          target: { kind: "task", id: pairId },
          params: { upload_id: uploadId },
        })}
        onDone={onAnnotationsImported}
        onError={onError}
      />
      <JobAction
        buttonClassName="menu-action"
        label="Скачать бэкап"
        shortLabel="бэкап"
        title="Скачать видео + разметку + паспорт задачи"
        disabled={disabled}
        request={() => ({
          type: "backup.task",
          target: { kind: "task", id: pairId },
        })}
        downloadName={`${pairId}-backup.zip`}
        onError={onError}
      />
    </div>
  );
}
