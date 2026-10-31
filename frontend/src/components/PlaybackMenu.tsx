import { formatNumber } from "../model/format";

interface Props {
  direction: 1 | -1;
  speed: number;
  onDirection: (direction: 1 | -1) => void;
  onSpeed: (speed: number) => void;
}

/** Направление и скорость: то, что раньше жило только в клавишах R и 0–9. */
export function PlaybackMenu({
  direction,
  speed,
  onDirection,
  onSpeed,
}: Props) {
  return (
    <div className="menu-stack">
      <div
        className="menu-segments"
        role="group"
        aria-label="Направление воспроизведения"
      >
        <button
          type="button"
          aria-pressed={direction === 1}
          onClick={() => onDirection(1)}
        >
          <span aria-hidden="true">▶</span> Вперёд
        </button>
        <button
          type="button"
          aria-pressed={direction === -1}
          onClick={() => onDirection(-1)}
        >
          <span aria-hidden="true">◀</span> Назад
        </button>
      </div>
      <label className="menu-slider">
        <span className="menu-slider-head">
          <span>Скорость</span>
          <b>x{formatNumber(speed)}</b>
        </span>
        <input
          type="range"
          min={0}
          max={9}
          step={1}
          value={Math.log2(speed)}
          aria-label="Скорость воспроизведения"
          onChange={(e) => onSpeed(2 ** parseInt(e.target.value, 10))}
        />
      </label>
      <p className="menu-note">
        Клавиши 0–9 задают скорость x1…x512, R меняет направление.
      </p>
    </div>
  );
}
