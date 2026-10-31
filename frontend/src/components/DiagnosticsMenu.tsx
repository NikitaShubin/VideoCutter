import { formatNumber } from "../model/format";
import { CacheControl } from "./CacheControl";

interface Props {
  fps: number;
  load: number;
  show: number;
}

/** Диагностика: кэш, замеры клиента и всё, что не нужно в рабочем ряду. */
export function DiagnosticsMenu({ fps, load, show }: Props) {
  const hasPerf = fps > 0 || load > 0 || show > 0;
  return (
    <div className="menu-stack">
      <CacheControl />
      {hasPerf ? (
        <div
          className="menu-readout"
          title="Замер клиента за секунду: к/с показа · мс загрузки кадра (сеть + сервер + декод браузера) · мс показа (рендер до отрисовки)."
        >
          <span className="menu-readout-label">Производительность</span>
          <span>
            ⏱ {formatNumber(fps)} к/с · ↓ {formatNumber(load)} мс · ⊙{" "}
            {formatNumber(show)} мс
          </span>
        </div>
      ) : (
        <p className="menu-note">
          Замеры появятся, когда вы начнёте листать кадры.
        </p>
      )}
    </div>
  );
}
