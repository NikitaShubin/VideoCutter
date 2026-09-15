import { useCallback, useEffect, useRef, useState } from "react";
import {
  frameUrl,
  getExportStatus,
  getPair,
  replaceFragments,
  startExport,
} from "../api";
import { FragmentModel } from "../model/fragmentModel";
import {
  FrameScheduler,
  nextPlayPosition,
  pickLoadTarget,
} from "../model/frameScheduler";
import type { ExportItem, VideoPairDetail } from "../types";

interface Props {
  pairId: string;
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
  //
  // Три режима показа (номер/полоса всегда обновляются мгновенно):
  //  - тапы (accumulate) → drain: показ каждого кадра по пути к позиции;
  //  - прыжки (таймлайн, PageUp/Down, )Home/End, одиночный Ctrl+→) → jump:
  //    сразу целевой кадр, без промежуточных;
  //  - удержание стрелки (e.repeat, в т.ч. Ctrl+) → chase: позиция бежит,
  //    экран догоняет, дропая кадры, с маркером до догона. Переход на
  //    удержание отменяет незаконченный буфер тапов (epoch++).
  const MAX_CACHE = 60;
  const SEEK_STEP = 10;
  const CHASE_CAP = 6;
  const schedRef = useRef<FrameScheduler | null>(null);
  if (!schedRef.current) schedRef.current = new FrameScheduler(MAX_CACHE);
  const [shownFrame, setShownFrame] = useState(-1);
  const modeRef = useRef<"drain" | "jump">("jump");
  const chaseRef = useRef(false);
  const heldDirRef = useRef<1 | -1>(1);
  const epochRef = useRef(0);
  const inflightRef = useRef<Set<number>>(new Set());
  const rightHeldRef = useRef(false);
  const leftHeldRef = useRef(false);

  // Загрузка пары + фрагментов.
  useEffect(() => {
    getPair(pairId).then((p) => {
      setPair(p);
      setPosition(0);
      schedRef.current = new FrameScheduler(MAX_CACHE);
      setShownFrame(-1);
      modeRef.current = "jump";
      chaseRef.current = false;
      epochRef.current++;
      inflightRef.current.clear();
      rightHeldRef.current = false;
      leftHeldRef.current = false;
      modelRef.current = new FragmentModel(p.total_frames);
      modelRef.current.setInitial(
        p.fragments.map((f) => ({ start: f.start, end: f.end, comment: f.comment ?? "" })),
      );
      rerender();
    });
  }, [pairId]);

  // Воспроизведение: позиция продвигается только ПОСЛЕ показа текущего кадра
  // (shownFrame === position), поэтому каждый кадр реально отображается,
  // без пропусков. Скорость задаёт паузу между кадрами (0-9 — прореживание).
  const totalFrames = pair?.total_frames ?? 0;
  useEffect(() => {
    if (!playing) return;
    if (shownFrame !== position) return; // ждём, пока текущий кадр встанет в <img>
    const delay = Math.round(1000 / (30 * speed));
    const id = window.setTimeout(() => {
      const step = nextPlayPosition(position, direction, totalFrames);
      if (step.stop) {
        // Дошли до начала/конца: разворачиваемся на воспроизведение
        // в обратную сторону, иначе пробел после остановки не запустит видео.
        setDirection(step.direction);
        setPlaying(false);
      } else {
        setPosition(step.pos);
      }
    }, delay);
    return () => window.clearTimeout(id);
  }, [playing, position, shownFrame, direction, speed, totalFrames]);

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

  // Показ кадра: ставим src, помечаем кадр как показанный (state — чтобы
  // эффекты и индикатор загрузки реагировали на появление кадра).
  const showFrame = useCallback(
    (idx: number) => {
      if (!pair) return;
      const el = imageRef.current;
      if (el) el.src = frameUrl(pair.id, idx);
      schedRef.current?.show(idx);
      setShownFrame(idx);
    },
    [pair],
  );

  // Единая точка загрузки/показа кадра. Вызывается реактивно (смена позиции
  // или показанного кадра) и императивно при смене режима навигации.
  const pump = useCallback(() => {
    const sched = schedRef.current;
    if (!pair || !sched) return;
    const chase = chaseRef.current;
    const pos = position;
    const shown = shownFrame;
    if (pos === shown) return;

    const target = chase
      ? pos
      : pickLoadTarget({ shown, position: pos, mode: modeRef.current });
    if (target === shown) return;

    // Дедупликация: кадр уже грузится.
    const inflight = inflightRef.current;
    if (inflight.has(target)) return;
    // Chase: ограничиваем поток одновременных запросов — остальные кадры
    // «пропускаются» (следующее изменение позиции запросит новее).
    if (chase && inflight.size >= CHASE_CAP) return;

    const { gen, cached } = sched.begin(target);
    if (cached) {
      showFrame(target);
      return;
    }

    const myEpoch = epochRef.current;
    const myChase = chase;
    inflight.add(target);
    const img = new Image();
    img.onload = () => {
      inflight.delete(target);
      // Режим сменился с момента запроса (chase/буфер тапов брошен, прыжок) —
      // ответ устарел, не показываем.
      if (myEpoch !== epochRef.current) return;
      if (myChase) {
        if (sched.deliverStreaming(target, img.src, heldDirRef.current)) {
          showFrame(target);
        }
      } else if (sched.deliver(target, gen, img.src)) {
        showFrame(target);
      }
    };
    img.onerror = () => {
      inflight.delete(target);
      if (myEpoch !== epochRef.current) return;
      // Кадр не загрузился: разблокируем покадровое воспроизведение/догон,
      // иначе воспроизведение залипнет на битом кадре.
      if (!myChase && gen === sched.generation) setShownFrame(pos);
    };
    img.src = frameUrl(pair.id, target);
  }, [pair, position, shownFrame, showFrame]);

  const pumpRef = useRef<() => void>(() => {});
  pumpRef.current = pump;

  useEffect(() => {
    pump();
  }, [pump]);

  // --- Режимы навигации (утдерживание vs тапы vs прыжки) ---
  // Прыжок: сразу целевой кадр, буфер/стрим отменяются.
  const jumpMode = () => {
    if (chaseRef.current || modeRef.current !== "jump") {
      epochRef.current++;
      inflightRef.current.clear();
    }
    chaseRef.current = false;
    modeRef.current = "jump";
    pumpRef.current();
  };

  // Тап: накопление — показ всех промежуточных кадров по пути (drain).
  const tapMode = () => {
    if (chaseRef.current || modeRef.current !== "drain") {
      epochRef.current++;
      inflightRef.current.clear();
    }
    chaseRef.current = false;
    modeRef.current = "drain";
    pumpRef.current();
  };

  // Удержание: real-time стрим с дропом, буфер тапов отбрасывается сразу.
  const startChase = (dir: 1 | -1) => {
    if (!chaseRef.current) {
      epochRef.current++;
      inflightRef.current.clear();
      chaseRef.current = true;
      modeRef.current = "jump"; // хвост после отпускания — строгая докачка
    }
    heldDirRef.current = dir;
    pumpRef.current();
  };

  // Отпускание: строгая докачка финального кадра до текущей позиции.
  const endChase = () => {
    if (!chaseRef.current) return;
    chaseRef.current = false;
    epochRef.current++;
    inflightRef.current.clear();
    modeRef.current = "jump";
    pumpRef.current();
  };

  // Отмена накопления (пробел — переход к воспроизведению).
  const cancelPending = () => {
    epochRef.current++;
    inflightRef.current.clear();
    modeRef.current = "jump";
    pumpRef.current();
  };

  const jump = (dir: 1 | -1) => {
    const p = modelRef.current;
    if (!p) return;
    jumpMode(); // прыжок без промежуточных кадров
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
      const rightKey = is("ArrowRight") || is("Period");
      const leftKey = is("ArrowLeft") || is("Comma");
      if (rightKey) rightHeldRef.current = true;
      if (leftKey) leftHeldRef.current = true;

      if (ctrl && rightKey) {
        setPlaying(false);
        if (e.repeat) startChase(1);
        else jumpMode(); // одиночный Ctrl+→ — прыжок на 10, без промежуточных
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + SEEK_STEP));
      } else if (ctrl && leftKey) {
        setPlaying(false);
        if (e.repeat) startChase(-1);
        else jumpMode();
        setPosition((pos) => Math.max(0, pos - SEEK_STEP));
      } else if (rightKey) {
        setPlaying(false);
        if (e.repeat) startChase(1);
        else tapMode();
        setPosition((pos) => Math.min(p.totalFrames - 1, pos + 1));
      } else if (leftKey) {
        setPlaying(false);
        if (e.repeat) startChase(-1);
        else tapMode();
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
        jumpMode();
        setPosition(0);
      } else if (is("End")) {
        setPlaying(false);
        jumpMode();
        setPosition(p.totalFrames - 1);
      } else if (is(" ") || is("Space") || is("Spacebar")) {
        e.preventDefault();
        cancelPending();
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
        saveToDb(p.getFragments());
      } else if (is("ArrowDown") || is("BracketRight")) {
        p.newEnd(position);
        rerender();
        drawStatusbar();
        saveToDb(p.getFragments());
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
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code === "ArrowRight" || e.code === "Period") rightHeldRef.current = false;
      if (e.code === "ArrowLeft" || e.code === "Comma") leftHeldRef.current = false;
      if (!rightHeldRef.current && !leftHeldRef.current) endChase();
    };
    const onBlur = () => {
      rightHeldRef.current = false;
      leftHeldRef.current = false;
      endChase();
    };
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
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

  // Индикатор загрузки — только при ручной навигации: при воспроизведении
  // кадр встаёт до продвижения позиции (покадровый шаг), индикатор не нужен.
  const frameLoading = !playing && position !== shownFrame;

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
          className={frameLoading ? "frame-loading" : undefined}
          style={{
            objectFit: preserveAspect ? "contain" : "fill",
            width: "100%",
            height: "100%",
          }}
        />
        {frameLoading && <div className="frame-spinner" aria-hidden />}
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
          jumpMode();
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