import { formatPercent } from "../model/format";

interface Props {
  scale: number;
  quality: number;
  preserveAspect: boolean;
  onScale: (value: number) => void;
  onQuality: (value: number) => void;
  onPreserveAspect: (value: boolean) => void;
  onFullscreen: () => void;
}

/** Настройки просмотра: то, что меняют редко, но хотят найти сразу. */
export function ViewMenu({
  scale,
  quality,
  preserveAspect,
  onScale,
  onQuality,
  onPreserveAspect,
  onFullscreen,
}: Props) {
  return (
    <div className="menu-stack">
      <label className="menu-slider">
        <span className="menu-slider-head">
          <span>Масштаб</span>
          <b>{formatPercent(Math.round(scale * 100))}</b>
        </span>
        <input
          type="range"
          min={5}
          max={100}
          step={5}
          value={Math.round(scale * 100)}
          aria-label="Масштаб кадров"
          onChange={(e) => onScale(parseInt(e.target.value, 10) / 100)}
        />
      </label>
      <label className="menu-slider">
        <span className="menu-slider-head">
          <span>Качество</span>
          <b>{quality}</b>
        </span>
        <input
          type="range"
          min={30}
          max={95}
          step={1}
          value={quality}
          aria-label="Качество JPEG"
          onChange={(e) => onQuality(parseInt(e.target.value, 10))}
        />
      </label>
      <div className="menu-separator" />
      <label className="menu-check">
        <input
          type="checkbox"
          checked={preserveAspect}
          onChange={(e) => onPreserveAspect(e.target.checked)}
        />
        <span>Сохранять пропорции кадра</span>
        <kbd>A</kbd>
      </label>
      <button type="button" className="menu-action" onClick={onFullscreen}>
        <span className="menu-action-title">
          <span>Во весь экран</span>
          <kbd>F</kbd>
        </span>
      </button>
    </div>
  );
}
