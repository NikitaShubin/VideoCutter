import { useEffect, useRef } from "react";
import { FragmentIcon } from "./FragmentIcon";
import { useKeyboardMode } from "./useKeyboardMode";

interface Props {
  open: boolean;
  onClose: () => void;
}

// Названия клавиш-модификаторов и комбинаций со склеенными «-».
function k(keys: string): string {
  return keys.split("+").map((k) => k.trim()).join("+");
}

export function HelpModal({ open, onClose }: Props) {
  const keyboard = useKeyboardMode();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    restoreRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    closeRef.current?.focus();

    const onKey = (e: KeyboardEvent) => {
      // Удержание не должно открывать/закрывать справку несколько раз.
      if (e.repeat) return;
      if (
        e.key === "Escape" ||
        e.key === "?" ||
        e.code === "KeyH" ||
        e.code === "KeyQ"
      ) {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), ' +
            'select:not([disabled]), textarea:not([disabled]), ' +
            '[tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.getAttribute("aria-hidden") !== "true");
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const previous = restoreRef.current;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="help-overlay" onClick={onClose}>
      <div
        ref={panelRef}
        className="help-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="help-header">
          <h2 id="help-title">Справка</h2>
          <button
            ref={closeRef}
            className="help-close"
            onClick={onClose}
            aria-label="Закрыть справку"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <nav className="help-toc" aria-label="Разделы справки">
          <a href="#help-frames">Кадры</a>
          <a href="#help-nav">{keyboard ? "Клавиши" : "Экран"}</a>
          <a href="#help-playback">Воспроизведение</a>
          <a href="#help-markup">Разметка</a>
          <a href="#help-view">Вид</a>
          <a href="#help-data">Данные</a>
          <a href="#help-diagnostics">Диагностика</a>
          <a href="#help-misc">Прочее</a>
          <a href="#help-timeline">Таймлайн</a>
        </nav>

        <p className="help-mode-note">
          {keyboard
            ? "Клавиатура: ниже — горячие клавиши."
            : "Сенсорный режим: горячие клавиши недоступны, ниже — экранные кнопки."}
        </p>

        <div className="help-section">
          <h3 id="help-frames">Навигация по кадрам</h3>
          <p>
            Номер кадра и полоса позиции обновляются <b>мгновенно</b> при
            любом вводе. Сам кадр грузится асинхронно и встаёт после загрузки;
            пока кадра нет или он меняется, в центре кадра крутится кольцо
            ожидания.
          </p>
          {keyboard ? (
            <ul className="help-modes">
              <li>
                <b>Тап</b> <span className="kbd">←</span>/<span className="kbd">→</span> —{" "}
                сразу целевой кадр. Серия быстрых тапов — тоже прыжок к позиции
                последнего: промежуточные кадры не показываются.
              </li>
              <li>
                <b>Удержание</b> стрелки (в т.ч. <span className="kbd">Ctrl</span>+стрелка) —{" "}
                <b>chase</b>: позиция бежит в реальном времени, экран догоняет
                её, пропуская кадры. Незаконченные запросы тапов при этом
                отменяются сразу.
              </li>
              <li>
                <b>Прыжки</b> — <span className="kbd">PageUp</span>/<span className="kbd">PageDown</span>,{" "}
                <span className="kbd">Home</span>/<span className="kbd">End</span>, одиночный{" "}
                <span className="kbd">Ctrl</span>+<span className="kbd">→</span>, клик по таймлайну:{" "}
                сразу целевой кадр, без промежуточных.
              </li>
              <li>
                <b>Воспроизведение</b> (<span className="kbd">Пробел</span>) — покадровое:
                каждый кадр обязательно показывается (без пропусков),
                воспроизведение останавливается в точности на показанном кадре.
              </li>
            </ul>
          ) : (
            <ul className="help-modes">
              <li>
                <b>Таймлайн</b> — коснитесь полосы, чтобы перейти к выбранному
                кадру.
              </li>
              <li>
                <b>Поле «кадр»</b> — введите номер и подтвердите: переход сразу
                к целевому кадру, без промежуточных.
              </li>
              <li>
                <b>Воспроизведение</b> — кнопка <b>▷ / ◁</b> запускает и
                ставит на паузу; направление и скорость — в меню <b>xN ▾</b>.
                Клик по самому кадру тоже переключает воспроизведение.
              </li>
            </ul>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-nav">Навигация</h3>
          {keyboard ? (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Клавиши</th><th scope="col">Действие</th></tr>
                <tr>
                  <td>
                    <span className="kbd">←</span> <span className="kbd">→</span>
                    {" "}(и{" "}<span className="kbd">,</span>/<span className="kbd">.</span>)
                  </td>
                  <td>назад / вперёд на 1 кадр (серия быстрых нажатий — прыжок к последней)</td>
                </tr>
                <tr>
                  <td>
                    {k("Ctrl+← / →")} (и{" "}
                    <span className="kbd">Ctrl</span>+<span className="kbd">,</span>/
                    <span className="kbd">.</span>)
                  </td>
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
                <tr>
                  <td>поле номера кадра (в полосе у таймлайна)</td>
                  <td>введите 1…N и нажмите Enter — переход без промежуточных кадров; Esc — отмена</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Экран</th><th scope="col">Действие</th></tr>
                <tr>
                  <td>таймлайн</td>
                  <td>коснитесь полосы, чтобы перейти к кадру</td>
                </tr>
                <tr>
                  <td>поле «кадр»</td>
                  <td>введите номер и подтвердите — переход без промежуточных кадров</td>
                </tr>
                <tr>
                  <td>кнопки ▷/◁ и xN ▾</td>
                  <td>воспроизведение, направление и скорость</td>
                </tr>
                <tr>
                  <td>сам кадр</td>
                  <td>коснитесь изображения — воспроизведение / пауза</td>
                </tr>
              </tbody>
            </table>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-playback">Воспроизведение</h3>
          <p>
            Кнопка <b>▷ / ◁</b> у таймлайна запускает и ставит на паузу;
            соседняя <b>xN ▾</b> открывает направление и скорость. Скорость
            — это шаг: x8 проходит восемь кадров за тик (прореживание для
            быстрого просмотра). Клик по кадру тоже запускает и
            останавливает показ.
          </p>
          {keyboard ? (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Клавиши</th><th scope="col">Действие</th></tr>
                <tr><td><span className="kbd">Пробел</span></td><td>воспроизведение / пауза</td></tr>
                <tr><td>{k("0 – 9")}</td><td>скорость воспроизведения (x1…x512)</td></tr>
                <tr><td><span className="kbd">R</span></td><td>направление: вперёд / назад</td></tr>
                <tr><td><span className="kbd">J</span></td><td>прокрутить до ближайшей границы фрагмента (начало/конец) и остановиться (на краю видео направление разворачивается)</td></tr>
              </tbody>
            </table>
          ) : (
            <p>
              В меню <b>xN ▾</b> — один переключатель направления
              «Вперёд / Назад» и ползунок скорости x1…x512. Коснитесь
              изображения, чтобы запустить или остановить показ.
            </p>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-markup">Разметка фрагментов</h3>
          {keyboard ? (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Клавиши</th><th scope="col">Действие</th></tr>
                <tr><td>{k("↑ / [ или ] / ↓")}</td><td>начало / конец фрагмента (ключевой кадр / граница)</td></tr>
                <tr><td><span className="kbd">K</span> <span className="kbd">Ins</span></td><td>вставить пару границ: первое нажатие — начало, второе — конец</td></tr>
                <tr><td>{k("D / Del")}</td><td>удалить фрагмент под позицией</td></tr>
                <tr><td><span className="kbd">I</span></td><td>комментарий к сегменту (Enter — новая строка, Ctrl+Enter — сохранить, Esc — отмена)</td></tr>
                <tr><td>{k("Ctrl+Z / Ctrl+Shift+Z / Z")}</td><td>отменить / повторить (Ctrl+Shift+Z или Z)</td></tr>
              </tbody>
            </table>
          ) : (
            <>
              <p>
                На сенсорном экране действия разметки — квадратные кнопки
                со значками в панели под шапкой. Вот что означает каждый
                значок.
              </p>
              <table className="help-table help-icon-table">
                <tbody>
                  <tr><th scope="col">Кнопка</th><th scope="col">Действие</th></tr>
                  <tr><td><FragmentIcon name="start" /></td><td><b>Начало фрагмента</b> — поставить начало в текущем кадре</td></tr>
                  <tr><td><FragmentIcon name="end" /></td><td><b>Конец фрагмента</b> — поставить конец в текущем кадре</td></tr>
                  <tr><td><FragmentIcon name="create" /></td><td><b>Создать</b> — первое нажатие ставит начало, второе (значок ✓) — конец</td></tr>
                  <tr><td><FragmentIcon name="complete" /></td><td><b>Завершить</b> — поставить конец, когда начало уже отмечено</td></tr>
                  <tr><td><FragmentIcon name="delete" /></td><td><b>Удалить</b> — убрать фрагмент под текущим кадром</td></tr>
                  <tr><td><FragmentIcon name="comment" /></td><td><b>Комментарий</b> — добавить или изменить комментарий к фрагменту</td></tr>
                  <tr><td><FragmentIcon name="undo" /></td><td><b>Отменить</b> — вернуть предыдущее состояние разметки</td></tr>
                  <tr><td><FragmentIcon name="redo" /></td><td><b>Повторить</b> — вернуть отменённое изменение</td></tr>
                </tbody>
              </table>
            </>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-view">Вид и интерфейс</h3>
          <p>
            Меню <b>«Вид»</b> в шапке редактора: масштаб и качество
            показываемых кадров, сохранение пропорций и полноэкранный режим.
            Настройки применяются с небольшой задержкой и запоминаются в
            задаче.
          </p>
          {keyboard ? (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Клавиши</th><th scope="col">Действие</th></tr>
                <tr><td><span className="kbd">F</span></td><td>полноэкранный режим</td></tr>
                <tr><td><span className="kbd">A</span></td><td>сохранять пропорции кадра / растягивать</td></tr>
                <tr>
                  <td>
                    <span className="kbd">V</span> / <span className="kbd">B</span> /{" "}
                    <span className="kbd">G</span>
                  </td>
                  <td>меню «Вид» / «Данные» / «Диагностика»; повторное нажатие закрывает его</td>
                </tr>
                <tr><td><span className="kbd">Esc</span></td><td>закрыть справку / комментарий / меню / список экспорта → выйти из полноэкранного режима → к списку</td></tr>
                <tr><td><span className="kbd">Q</span></td><td>назад к списку пар (позиция кадра запоминается)</td></tr>
                <tr><td>{k("H или ?")}</td><td>показать / скрыть эту справку (на любой раскладке)</td></tr>
              </tbody>
            </table>
          ) : (
            <p>
              Полноэкранный режим включается в меню <b>«Вид»</b>; там же
              сохраняются пропорции кадра. Чтобы вернуться к списку задач,
              нажмите <b>«← Назад»</b> в шапке.
            </p>
          )}
          {keyboard && (
            <p>
              Пока меню открыто, глобальные клавиши редактора выключены;{" "}
              <span className="kbd">Esc</span> или повторное нажатие той же
              клавиши закрывает его и возвращает управление. Повторяются
              только стрелки, «,»/«.» и{" "}
              <span className="kbd">PageUp</span>/<span className="kbd">PageDown</span>;
              остальные клавиши срабатывают один раз. Из модификаторов работают
              только <span className="kbd">Ctrl</span>+<span className="kbd">Z</span>,{" "}
              <span className="kbd">Ctrl</span>+<span className="kbd">Shift</span>+
              <span className="kbd">Z</span>,{" "}
              <span className="kbd">Ctrl</span>+<span className="kbd">Enter</span> и{" "}
              <span className="kbd">Ctrl</span>+<span className="kbd">←</span>/
              <span className="kbd">→</span>: прочие сочетания остаются браузеру.
            </p>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-data">Данные: разметка и бэкапы</h3>
          <p>
            Меню <b>«Данные»</b> в шапке редактора: <b>Скачать разметку</b> —
            экспорт разметки задачи (fragments.tsv + манифест, без видео);
            <b> Импортировать разметку</b> — применить такой архив к задаче
            (чужое видео отклоняется сверкой кадров); <b>Скачать бэкап</b> —
            экспорт полного слепка задачи (видео + разметка + паспорт).
            Восстановление любого архива — «Восстановить из бэкапа…» на
            вкладке «Проекты» (совпадающие имена получают суффикс{" "}
            <span className="kbd">_1</span>).
          </p>
        </div>

        <div className="help-section">
          <h3 id="help-diagnostics">Диагностика</h3>
          <p>
            Меню <b>«Диагностика»</b> показывает заполнение GOP-кэша и
            позволяет менять его лимиты без перезапуска, а также замеры
            клиента (кадры в секунду, загрузка и показ кадра). Кэш
            опрашивается только пока меню открыто.
          </p>
        </div>

        <div className="help-section">
          <h3 id="help-misc">Прочее</h3>
          {keyboard ? (
            <table className="help-table">
              <tbody>
                <tr><th scope="col">Клавиша</th><th scope="col">Действие</th></tr>
                <tr><td><span className="kbd">E</span></td><td>экспорт фрагментов (оригиналы) — кнопка «Экспорт» в шапке редактора; повтор во время экспорта — отмена</td></tr>
              </tbody>
            </table>
          ) : (
            <p>
              Кнопка <b>«Экспорт»</b> в шапке экспортирует фрагменты
              (оригиналы); повторное нажатие во время экспорта отменяет его.
            </p>
          )}
        </div>

        <div className="help-section">
          <h3 id="help-timeline">Таймлайн</h3>
          <div className="help-legend">
            <span><i className="help-swatch" style={{ background: "var(--tl-green)" }} /> фон — вся последовательность кадров</span>
            <span><i className="help-swatch" style={{ background: "#0077ff" }} /> синий фон — отправка файла</span>
            <span><i className="help-swatch" style={{ background: "#00bebe" }} /> бирюза — приём сервером (ложится поверх синего)</span>
            <span><i className="help-swatch" style={{ background: "#8b5cf6" }} /> фиолет — проверка/индексация поверх предыдущего (не разметка)</span>
            <span><i className="help-swatch" style={{ background: "#ff0000" }} /> фрагмент</span>
            <span><i className="help-swatch" style={{ background: "#ff8c00" }} /> фрагмент с комментарием</span>
            <span><i className="help-swatch" style={{ background: "rgba(0,0,0,0.5)" }} /> затемнение — кадры после текущего</span>
            <span><i className="help-swatch" style={{ background: "#ffffff" }} /> маркер текущего кадра</span>
            <span><i className="help-swatch" style={{ background: "#00ffff" }} /> выбранный диапазон (на зелёном)</span>
            <span><i className="help-swatch" style={{ background: "#ff00ff" }} /> выбранный диапазон (на фрагменте)</span>
          </div>
          <p>
            В списке задач таймлайн — тонкая полоса под названием задачи;
            цвета те же, что в редакторе. На десктопе значения цветов — в
            подсказке при наведении на полосу; на сенсорном экране — в этой
            таблице.
          </p>
        </div>
      </div>
    </div>
  );
}