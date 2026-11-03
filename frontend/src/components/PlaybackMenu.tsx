import { formatNumber } from "../model/format";

interface Props {
  direction: 1 | -1;
  speed: number;
  /** Клавиатурные подсказки: на сенсорном экране их нечем выполнить. */
  showShortcuts: boolean;
  onDirection: (direction: 1 | -1) => void;
  onSpeed: (speed: number) => void;
}

/** Направление и скорость: то, что раньше жило только в клавишах R и 0–9. */
export function PlaybackMenu({
  direction,
  speed,
  showShortcuts,
  onDirection,
  onSpeed,
}: Props) {
  return (
    <div className="menu-stack">
      <div className="menu-direction">
        <span className="menu-direction-label">Направление</span>
        <button
          type="button"
          aria-label={`Направление: ${direction === -1 ? "назад" : "вперёд"}. Сменить`}
          title={
            showShortcuts
              ? "Сменить направление (R)"
              : "Сменить направление"
          }
          onClick={() => onDirection(direction === 1 ? -1 : 1)}
        >
          <span aria-hidden="true">{direction === -1 ? "◀" : "▶"}</span>
          {direction === -1 ? "Назад" : "Вперёд"}
          {showShortcuts && <kbd>R</kbd>}
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
        {showShortcuts
          ? "Клавиши 0–9 задают скорость x1…x512, R меняет направление."
          : "Скорость и направление задаются ползунком и кнопкой."}
      </p>
    </div>
  );
}
