import { useCallback, useEffect, useRef, useState } from "react";
import {
  frameUrl,
  getExportStatus,
  getPair,
  replaceFragments,
  startExport,
} from "../api";
import { FragmentModel } from "../model/fragmentModel";
import type { ExportItem, VideoPairDetail } from "../types";

interface Props {
  pairId: number;
  onBack: () => void;
}

export function CutEditor({ pairId, onBack }: Props) {
  const [pair, setPair] = useState<VideoPairDetail | null>(null);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [direction, setDirection] = useState<1 | -1>(1);
  const [keyPose, setKeyPose] = useState<number | null>(null);
  const [preserveAspect, setPreserveAspect] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [exportItems, setExportItems] = useState<ExportItem[] | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [editingComment, setEditingComment] = useState(false);
  const [commentDraft, setCommentDraft] = useState("");

  const modelRef = useRef<FragmentModel | null>(null);
  if (!modelRef.current) modelRef.current = new FragmentModel(0);
  const [, forceRender] = useState(0);
  const rerender = () => forceRender((n) => n + 1);

  const statusRef = useRef<HTMLCanvasElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const editorRef = useRef<HTMLDivElement | null>(null);
  const timerRef = useRef<number | null>(null);

  // Синхронизация состояния с полноэкранным режимом браузера (вкл/выкл через Esc).
  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === editorRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      editorRef.current?.requestFullscreen().catch(() => {});
    }
  };

  // Клиентский кэш кадров + предзагрузка: кадр подставляется в <img> только
  // после загрузки, поэтому пустой/чёрный экран при перемотке не моргает.
  const shownFrameRef = useRef(-1);
  const wantedFrameRef = useRef(-1);
  const frameCacheRef = useRef<Map<number, string>>(new Map());
  const inFlightRef = useRef<Map<number, HTMLImageElement>>(new Map());
  const MAX_CACHE = 60;
  const SEEK_STEP = 10;

  // Загрузка пары + фрагментов.
  useEffect(() => {
    getPair(pairId).then((p) => {
      setPair(p);
      setPosition(0);
      shownFrameRef.current = -1;
      wantedFrameRef.current = -1;
      frameCacheRef.current.clear();
      inFlightRef.current.clear();
      modelRef.current = new FragmentModel(p.total_frames);
      modelRef.current.setInitial(
        p.fragments.map((f) => ({ start: f.start, end: f.end, comment: f.comment ?? "" })),
      );
      rerender();
    });
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [pairId]);

  // Таймер воспроизведения.
  useEffect(() => {
    if (!playing) return;
    timerRef.current = window.setInterval(() => {
      const p = modelRef.current;
      if (!p) return;
      setPosition((pos) => {
        const next = pos + direction * speed;
        if (next < 0) {
          // Дошли до начала: разворачиваемся на воспроизведение вперёд,
          // иначе пробел после остановки не запустит видео обратно.
          setDirection(1);
          setPlaying(false);
          return 0;
        }
        if (next >= p.totalFrames) {
          setDirection(-1);
          setPlaying(false);
          return p.totalFrames - 1;
        }
        return next;
      });
    }, 30);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [playing, speed, direction]);

  // Статус-бар (таймлайн). Воспроизводит draw_statusbar из PyVideoCutter:
  // зелёный фон, красные фрагменты, затемнение от позиции до конца,
  // выбранный диапазон добавляет синий канал (globalCompositeOperation).
  const drawStatusbar = useCallback(() => {
    const canvas = statusRef.current;
    const p = modelRef.current;
    if (!canvas || !p) return;
    const width = canvas.width;
    const height = canvas.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Билинейная обратимая пара: [0,total-1] -> [0,width-1].
    // (width-1)*frame/total не достигает последнего пикселя при последнем кадре;
    // оригинал для позиции использовал int(width*position/total) — без "-1".
    const span = Math.max(1, p.totalFrames - 1);
    const sbX = (frame: number) => Math.round(frame * (width - 1) / span);

    // Фон — зелёный (BGR (0,255,0)).
    ctx.fillStyle = "#00ff00";
    ctx.fillRect(0, 0, width, height);

    // Фрагменты — красные (BGR (255,0,0)). Правая граница включительно,
    // чтобы последний кадр доходил до правого края canvas.
    ctx.fillStyle = "#ff0000";
    for (const f of p.fragments) {
      const x = sbX(f.start);
      const endX = sbX(f.end);
      ctx.fillRect(x, 0, Math.max(1, endX - x + 1), height);
    }

    // Затемнение от текущей позиции до конца (sb[:, current_shift:, :] //= 2).
    const curX = sbX(position);
    ctx.fillStyle = "rgba(0,0,0,0.5)";
    ctx.fillRect(curX, 0, width - curX, height);

    // Выбранный диапазон (key pose → position): добавляем синий канал = 255,
    // как в оригинале (sb[:, start:end, 0] = 255). "lighter" складывает каналы:
    // зелёный → циан, красный → пурпурный.
    if (keyPose !== null) {
      const [s, e] = [Math.min(keyPose, position), Math.max(keyPose, position)];
      const x = sbX(s);
      const endX = sbX(e);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.fillStyle = "#0000ff";
      ctx.fillRect(x, 0, endX - x, height);
      ctx.restore();
    }
  }, [keyPose, position]);

  useEffect(() => {
    drawStatusbar();
  }, [drawStatusbar, position, keyPose, pair]);

  // Показ кадра: ставим src только когда кадр полностью загружен и декодирован.
  const applyFrame = useCallback(
    (idx: number) => {
      const el = imageRef.current;
      if (!pair || !el) return;
      shownFrameRef.current = idx;
      el.src = frameUrl(pair.id, idx);
    },
    [pair],
  );

  useEffect(() => {
    if (!pair || position === shownFrameRef.current) return;
    const target = position;
    wantedFrameRef.current = target;

    const cached = frameCacheRef.current.get(target);
    if (cached) {
      applyFrame(target);
      return;
    }

    // Не запускаем второй запрос, если какой-то кадр уже в полёте:
    // при непрерывном воспроизведении позиция меняется быстрее, чем
    // приходит ответ, и параллельные запросы забивают сеть.
    if (inFlightRef.current.size > 0) return;

    const img = new Image();
    inFlightRef.current.set(target, img);
    img.onload = () => {
      inFlightRef.current.delete(target);
      frameCacheRef.current.set(target, img.src);
      if (frameCacheRef.current.size > MAX_CACHE) {
        const oldest = frameCacheRef.current.keys().next().value;
        if (oldest !== undefined) frameCacheRef.current.delete(oldest);
      }
      // Показываем кадр, если он ближе к цели, чем уже показанный
      // (при воспроизведении wanted всё время «убегает» вперёд, и точное
      // равенство требовало бы перемотку назад — кадр бы никогда не встал).
      const want = wantedFrameRef.current;
      const shown = shownFrameRef.current;
      const isCloser = Math.abs(want - target) < Math.abs(want - shown);
      if (isCloser && shown !== target) {
        applyFrame(target);
      }
    };
    img.onerror = () => inFlightRef.current.delete(target);
    img.src = frameUrl(pair.id, target);
  }, [position, pair, applyFrame]);

  const jump = (dir: 1 | -1) => {
    const p = modelRef.current;
    if (!p) return;
    const ks = [0, ...p.keyFrames(), p.totalFrames - 1];
    if (dir === 1) {
      for (const f of ks) {
        if (f > position) {
          setPosition(f);
          return;
        }
      }
      setPosition(ks[ks.length - 1]);
    } else {
      for (let i = ks.length - 1; i >= 0; i--) {
        if (ks[i] < position) {
          setPosition(ks[i]);
          return;
        }
      }
      setPosition(ks[0]);
    }
  };

  const flash = (m: string) => {
    setMessage(m);
    window.setTimeout(() => setMessage(""), 2000);
  };

  const commentRef = useRef<HTMLTextAreaElement | null>(null);
  const committedRef = useRef(false);

  const startCommentEdit = () => {
    const p = modelRef.current;
    if (!p) return;
    const f = p.fragmentAt(position);
    if (!f) {
      flash("Комментарии можно добавлять только к выделенным участкам");
      return;
    }
    setCommentDraft(f.comment);
    setEditingComment(true);
    committedRef.current = false;
    window.setTimeout(() => commentRef.current?.focus(), 0);
  };

  const commitComment = () => {
    const p = modelRef.current;
    if (!p || committedRef.current) return;
    committedRef.current = true;
    setEditingComment(false);
    if (p.setComment(position, commentDraft)) {
      rerender();
      drawStatusbar();
      saveToDb(p.getFragments());
    }
  };

  // Клавиатура.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const p = modelRef.current;
      if (!p) return;
      const k = e.key;
      const code = e.code;
      const ctrl = e.ctrlKey || e.metaKey;
      // Физическая клавиша: кнопка не зависит от раскладки (KeyR ~ R на любой раскладке).
      const is = (physical: string) => code === physical || k === physical;

      const isEditing = (e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "TEXTAREA";
      if (isEditing) return;

      // Навигация.
      if (ctrl && (is("ArrowRight") || is("Period"))) {
        setPlaying(false);
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + SEEK_STEP));
      } else if (ctrl && (is("ArrowLeft") || is("Comma"))) {
        setPlaying(false);
        setPosition((pos) => Math.max(0, pos - SEEK_STEP));
      } else if (is("ArrowRight") || is("Period")) {
        setPlaying(false);
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + 1));
      } else if (is("ArrowLeft") || is("Comma")) {
        setPlaying(false);
        setPosition((pos) => Math.max(0, pos - 1));
      } else if (is("PageDown")) {
        e.preventDefault();
        setPlaying(false);
        jump(1);
      } else if (is("PageUp")) {
        e.preventDefault();
        setPlaying(false);
        jump(-1);
      } else if (is("Home")) {
        setPlaying(false);
        setPosition(0);
      } else if (is("End")) {
        setPlaying(false);
        setPosition(p.totalFrames - 1);
      } else if (is(" ") || is("Space") || is("Spacebar")) {
        e.preventDefault();
        setPlaying((v) => !v);
      } else if (is("KeyR")) {
        setDirection((d) => (d === 1 ? -1 : 1));
      } else if (is("KeyJ")) {
        jump(direction);
      } else if (/^Digit[0-9]$/.test(code)) {
        setSpeed(Math.pow(2, parseInt(code.slice(5), 10)));
      } else if (is("KeyF")) {
        toggleFullscreen();
      } else if (is("Tab")) {
        e.preventDefault();
        setPreserveAspect((v) => !v);
      }

      // Отметка фрагментов.
      else if (is("ArrowUp") || is("BracketLeft")) {
        p.newStart(position);
        rerender();
        drawStatusbar();
      } else if (is("ArrowDown") || is("BracketRight")) {
        p.newEnd(position);
        rerender();
        drawStatusbar();
      } else if (is("KeyK") || is("Insert") || is("F12")) {
        if (keyPose === null) {
          if (p.fragments.some((f) => position >= f.start && position <= f.end)) {
            flash("Невозможно создать фрагмент внутри другого");
          } else {
            setKeyPose(position);
          }
        } else {
          const [lo, hi] = keyPose < position
            ? [keyPose, position]
            : [position, keyPose];
          const ok = p.add(lo, hi);
          if (ok) {
            setKeyPose(null);
            rerender();
            drawStatusbar();
            saveToDb(p.getFragments());
          } else {
            flash("Фрагмент пересекается с существующим");
          }
        }
      } else if (is("Delete") || is("KeyD")) {
        const res = p.delete(position);
        if (res === "ok" || res === "merged") {
          rerender();
          drawStatusbar();
          saveToDb(p.getFragments());
          if (res === "merged") startCommentEdit();
        } else if (res === "ambiguous") {
          flash("Текущий кадр — стык двух фрагментов. Переместите курсор.");
        }
      } else if (is("KeyI")) {
        startCommentEdit();
      } else if (ctrl && is("KeyZ")) {
        p.undo();
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("KeyZ")) {
        p.redo();
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
      } else if (is("KeyE")) {
        handleExport();
      } else if (is("Escape") || is("KeyQ")) {
        if (!document.fullscreenElement) onBack();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position, keyPose, direction, speed, playing, drawStatusbar]);

  const saveToDb = (frags: { start: number; end: number; comment: string }[]) => {
    replaceFragments(pairId, frags)
      .then(() => flash("Сохранено"))
      .catch((e) => flash(`Ошибка сохранения: ${e.message}`));
  };

  const handleExport = async () => {
    setExporting(true);
    setExportProgress(0);
    const pollTimer = window.setInterval(async () => {
      try {
        const st = await getExportStatus(pairId);
        if (st.state === "running") {
          const total = st.total ?? 1;
          setExportProgress((st.index ?? 0) / Math.max(1, total));
        } else {
          window.clearInterval(pollTimer);
          setExporting(false);
          setExportProgress(null);
          if (st.state === "done") {
            setExportItems(st.files ?? []);
            flash(`Экспортировано фрагментов: ${st.files?.length ?? 0}`);
          } else if (st.state === "error") {
            flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`);
          }
        }
      } catch (e) {
        window.clearInterval(pollTimer);
        setExporting(false);
        setExportProgress(null);
        flash(`Ошибка опроса экспорта: ${(e as Error).message}`);
      }
    }, 500);
    try {
      const st = await startExport(pairId);
      if (st.state !== "running") {
        window.clearInterval(pollTimer);
        setExporting(false);
        setExportProgress(null);
        if (st.state === "error") flash(`Ошибка экспорта: ${st.error ?? "неизвестно"}`);
        else flash("Экспорт не запущен");
      }
    } catch (e) {
      window.clearInterval(pollTimer);
      setExporting(false);
      setExportProgress(null);
      flash(`Ошибка экспорта: ${(e as Error).message}`);
    }
  };

  if (!pair) {
    return <div className="loading">Загрузка видео-пары…</div>;
  }

  const sel = modelRef.current?.selectedFrames() ?? 0;

  return (
    <div ref={editorRef} className={isFullscreen ? "editor fullscreen" : "editor"}>
      {!isFullscreen && (
        <div className="editor-toolbar">
          <button onClick={onBack}>← Назад</button>
          <span className="pair-name">{pair.original_name} → {pair.visualization_name || "оригинал"}</span>
          <span className="info">
            кадр {position + 1}/{pair.total_frames} · показано {sel} ({(100 * sel / pair.total_frames).toFixed(1)}%)
          </span>
          <span className="info">скорость: {speed}· {direction === -1 ? "[назад]" : "[вперёд]"}</span>
          <button
            className="toolbar-export"
            onClick={() => handleExport()}
            disabled={exporting}
          >
            {exportProgress !== null && (
              <span
                className="toolbar-export-progress"
                style={{ width: `${Math.round(exportProgress * 100)}%` }}
              />
            )}
            <span className="toolbar-export-label">
              {exportProgress !== null
                ? `${Math.round(exportProgress * 100)}%`
                : "Экспорт (E)"}
            </span>
          </button>
        </div>
      )}

      <div className="video-area">
        <img
          ref={imageRef}
          alt={`кадр ${position}`}
          style={{
            objectFit: preserveAspect ? "contain" : "fill",
            width: "100%",
            height: "100%",
          }}
        />
        {(!isFullscreen || editingComment) && (
          editingComment ? (
            <div className="editor-comment">
              <textarea
                ref={commentRef}
                rows={3}
                value={commentDraft}
                placeholder="Комментарий к сегменту…"
                onChange={(e) => setCommentDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setEditingComment(false);
                  } else if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    commitComment();
                  }
                }}
                onBlur={commitComment}
              />
            </div>
          ) : modelRef.current?.fragmentAt(position)?.comment ? (
            <div className="editor-comment">
              <span className="comment-text">
                {modelRef.current?.fragmentAt(position)?.comment}
              </span>
            </div>
          ) : null
        )}
      </div>

      <canvas
        ref={statusRef}
        className="statusbar"
        width={800}
        height={20}
        onClick={(e) => {
          const p = modelRef.current;
          const canvas = e.currentTarget;
          if (!p) return;
          const rect = canvas.getBoundingClientRect();
          const x = (e.clientX - rect.left) * (canvas.width / rect.width);
          const pixel = Math.min(canvas.width - 1, Math.max(0, x));
          const target = Math.min(
            p.totalFrames - 1,
            Math.round(pixel * Math.max(1, p.totalFrames - 1) / (canvas.width - 1)),
          );
          setPlaying(false);
          setPosition(target);
        }}
      />

      {!isFullscreen && (
        <div className="editor-footer">
          <span>←/→ — кадр, Ctrl+←/→ — 10 кадров, PageUp/PageDown — границы фрагментов, Home/End — край видео,</span>
          <span>Пробел — play/pause, 0-9 — скорость, R — направление, клик по таймлайну — переход,</span>
          <span>F — полноэкранный режим, Tab — сохранение пропорций, Esc — выход из полноэкранного режима,</span>
          <span>↑/[/↓/] — границы фрагмента, K — пара границ, D/Del — удалить, I — комментарий к сегменту, Ctrl+Z/Z — undo/redo, E — экспорт</span>
        </div>
      )}
      {!isFullscreen && message && <div className="flash">{message}</div>}

      {!isFullscreen && exportItems && (
        <div className="export-list">
          <h3>Фрагменты экспортированы (оригиналы):</h3>
          <ul>
            {exportItems.map((f) => (
              <li key={f.url}>
                <a href={f.url} download>{f.filename}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}