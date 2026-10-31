import { formatPercent } from "../model/format";
import { FileButton } from "./FileButton";

interface Props {
  downloading: "ann" | "bak" | null;
  downloadFrac: number | null;
  annBusy: boolean;
  annProgress: number | null;
  onDownload: (key: "ann" | "bak") => void;
  onImport: (file: File) => void;
}

function progressLabel(base: string, frac: number | null): string {
  return frac === null
    ? `${base}…`
    : `${base} ${formatPercent(Math.round(frac * 100))}`;
}

/** Данные: разметка и бэкапы — три файловых действия с пояснениями. */
export function DataMenu({
  downloading,
  downloadFrac,
  annBusy,
  annProgress,
  onDownload,
  onImport,
}: Props) {
  const busy = downloading !== null || annBusy;
  const annDownloading = downloading === "ann";
  const bakDownloading = downloading === "bak";

  return (
    <div className="menu-stack">
      <button
        type="button"
        className="menu-action"
        disabled={busy}
        onClick={() => onDownload("ann")}
      >
        <span className="menu-action-title">
          {annDownloading
            ? progressLabel("Скачиваю разметку", downloadFrac)
            : "Скачать разметку"}
        </span>
        <span className="menu-action-note">
          fragments.tsv + манифест, без видео
        </span>
      </button>
      <FileButton
        className="menu-action"
        accept=".zip,application/zip"
        disabled={annBusy}
        onFile={onImport}
      >
        <span className="menu-action-title">
          {annProgress !== null
            ? `Импортирую разметку ${formatPercent(Math.round(annProgress * 100))}`
            : "Импортировать разметку"}
        </span>
        <span className="menu-action-note">
          применить архив к задаче; видео не меняется
        </span>
      </FileButton>
      <button
        type="button"
        className="menu-action"
        disabled={busy}
        onClick={() => onDownload("bak")}
      >
        <span className="menu-action-title">
          {bakDownloading
            ? progressLabel("Скачиваю бэкап", downloadFrac)
            : "Скачать бэкап"}
        </span>
        <span className="menu-action-note">
          видео + разметка + паспорт задачи
        </span>
      </button>
    </div>
  );
}
