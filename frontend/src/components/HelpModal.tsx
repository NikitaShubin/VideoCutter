interface Props {
  open: boolean;
  onClose: () => void;
}

// Названия клавиш-модификаторов и комбинаций со склеенными «-».
function k(keys: string): string {
  return keys.split("+").map((k) => k.trim()).join("+");
}

export function HelpModal({ open, onClose }: Props) {
  if (!open) return null;
  return (
    <div className="help-overlay" onClick={onClose}>
      <div
        className="help-panel"
        role="dialog"
        aria-label="Справка"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="help-header">
          <h2>Справка</h2>
          <button className="help-close" onClick={onClose} aria-label="Закрыть справку">
            ✕
          </button>
        </div>

        <div className="help-section">
          <h3>Навигация по кадрам</h3>
          <p>
            Номер кадра и полоса позиции обновляются <b>мгновенно</b> при
            любом вводе. Сам кадр грузится асинхронно и встаёт после загрузки;
            пока кадр не показан, в правом нижнем углу крутится индикатор.
            В зависимости от способа управления используется один из режимов:
          </p>
          <ul className="help-modes">
            <li>
              <b>Тап</b> <span className="kbd">←</span>/<span className="kbd">→</span> —{" "}
              <b>drain</b>: показываются все промежуточные кадры по пути к
              целевому. Быстрые тапы накапливаются во время загрузки и затем
              отдаются по одному, без пропусков.
            </li>
            <li>
              <b>Удержание</b> стрелки (в т.ч. <span className="kbd">Ctrl</span>+стрелка) —{" "}
              <b>chase</b>: позиция бежит в реальном времени, экран догоняет
              её, пропуская кадры. Переход на удержание сразу отменяет
              незаконченную очередь тапов.
            </li>
            <li>
              <b>Прыжки</b> — <span className="kbd">PageUp</span>/<span className="kbd">PageDown</span>,{" "}
              <span className="kbd">Home</span>/<span className="kbd">End</span>, одиночный{" "}
              <span className="kbd">Ctrl</span>+<span className="kbd">→</span>, клик по таймлайну:{" "}
              сразу целевой кадр, без промежуточных.
            </li>
            <li>
              <b>Воспроизведение</b> (<span className="kbd">Пробел</span>) — покадровое:
              каждый кадр обязательно показывается (без пропусков), остановка
              останавливается в точности на показанном кадре.
            </li>
          </ul>
        </div>

        <div className="help-section">
          <h3>Навигация</h3>
          <table className="help-table">
            <tbody>
              <tr><th>Клавиши</th><th>Действие</th></tr>
              <tr>
                <td><span className="kbd">←</span> <span className="kbd">→</span></td>
                <td>назад / вперёд на 1 кадр (одиночное нажатие — покадровый drain)</td>
              </tr>
              <tr>
                <td>{k("Ctrl + ← / →")}</td>
                <td>10 кадров (одиночное нажатие — прыжок, удержание — догон)</td>
              </tr>
              <tr>
                <td>{k("PageDown / PageUp")}</td>
                <td>к следующей / предыдущей границе фрагментов</td>
              </tr>
              <tr>
                <td>{k("Home / End")}</td>
                <td>первый / последний кадр видео</td>
              </tr>
              <tr>
                <td>клик по таймлайну</td>
                <td>переход к выбранному кадру</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div className="help-section">
          <h3>Воспроизведение</h3>
          <table className="help-table">
            <tbody>
              <tr><th>Клавиши</th><th>Действие</th></tr>
              <tr><td><span className="kbd">Пробел</span></td><td>play / pause</td></tr>
              <tr><td>{k("0 – 9")}</td><td>скорость воспроизведения</td></tr>
              <tr><td><span className="kbd">R</span></td><td>направление: вперёд / назад</td></tr>
              <tr><td><span className="kbd">J</span></td><td>прокрутить до ближайшей границы фрагмента (начало/конец) и остановиться</td></tr>
            </tbody>
          </table>
        </div>

        <div className="help-section">
          <h3>Разметка фрагментов</h3>
          <table className="help-table">
            <tbody>
              <tr><th>Клавиши</th><th>Действие</th></tr>
              <tr><td>{k("↑ / ] или [ / ↓")}</td><td>начало / конец фрагмента (ключевой кадр / граница)</td></tr>
              <tr><td><span className="kbd">K</span></td><td>вставить пару границ вокруг текущей позиции</td></tr>
              <tr><td>{k("D / Del")}</td><td>удалить фрагмент под позицией</td></tr>
              <tr><td><span className="kbd">I</span></td><td>комментарий к сегменту (Enter — сохранить, Esc — отмена)</td></tr>
              <tr><td>{k("Ctrl+Z / Z")}</td><td>undo / redo</td></tr>
            </tbody>
          </table>
        </div>

        <div className="help-section">
          <h3>Вид и интерфейс</h3>
          <table className="help-table">
            <tbody>
              <tr><th>Клавиши</th><th>Действие</th></tr>
              <tr><td><span className="kbd">F</span></td><td>полноэкранный режим</td></tr>
              <tr><td><span className="kbd">Tab</span></td><td>сохранять пропорции кадра / растягивать</td></tr>
              <tr><td><span className="kbd">Esc</span></td><td>закрыть справку / выйти из полноэкранного режима</td></tr>
              <tr><td>{k("Esc или Q")}</td><td>назад к списку пар (вне полноэкранного режима)</td></tr>
              <tr><td>{k("H или ?")}</td><td>показать / скрыть эту справку</td></tr>
            </tbody>
          </table>
        </div>

        <div className="help-section">
          <h3>Прочее</h3>
          <table className="help-table">
            <tbody>
              <tr><th>Клавиша</th><th>Действие</th></tr>
              <tr><td><span className="kbd">E</span></td><td>экспорт фрагментов (оригиналы) — кнопка «Экспорт» в панели</td></tr>
            </tbody>
          </table>
        </div>

        <div className="help-section">
          <h3>Таймлайн</h3>
          <div className="help-legend">
            <span><i className="help-swatch" style={{ background: "#00ff00" }} /> фон — вся последовательность кадров</span>
            <span><i className="help-swatch" style={{ background: "#ff0000" }} /> фрагмент</span>
            <span><i className="help-swatch" style={{ background: "rgba(0,0,0,0.5)" }} /> затемнение до конца видео</span>
            <span><i className="help-swatch" style={{ background: "#00ffff" }} /> выбранный диапазон (на зелёном)</span>
            <span><i className="help-swatch" style={{ background: "#ff00ff" }} /> выбранный диапазон (на фрагменте)</span>
          </div>
        </div>
      </div>
    </div>
  );
}