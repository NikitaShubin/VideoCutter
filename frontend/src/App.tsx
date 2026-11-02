import {
  type ReactElement,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  authStatus,
  clearAuthToken,
  getAuthToken,
  getWorkspacesStatus,
  listPairs,
  setUnauthorizedHandler,
} from "./api";
import { PairList } from "./components/PairList";
import { CutEditor, type BackGuard } from "./components/CutEditor";
import { LoginScreen } from "./Login";
import { useUploadManager } from "./model/uploads";
import type { VideoPair } from "./types";

type Screen = "list" | "editor";

interface Gate {
  checked: boolean;
  enabled: boolean;
  authed: boolean;
}

export function App() {
  const [screen, setScreen] = useState<Screen>("list");
  const [pairs, setPairs] = useState<VideoPair[]>([]);
  const [pairId, setPairId] = useState<string | null>(null);
  // Входная дверь: закрыта — экран токена вместо списка.
  const [gate, setGate] = useState<Gate>({
    checked: false,
    enabled: false,
    authed: false,
  });

  const reload = useCallback(() => {
    listPairs().then(setPairs).catch(console.error);
  }, []);

  // Экран живёт в state, но вход в редактор оставляет запись в history:
  // кнопка «Назад» браузера возвращает к списку, а не выбрасывает из
  // приложения. popstate — единственный источник правды для перехода назад.
  // Свежие screen/pairId нужны обработчику popstate без пересоздания
  // подписки на каждое изменение.
  const screenRef = useRef<Screen>("list");
  screenRef.current = screen;
  const pairIdRef = useRef<string | null>(null);
  pairIdRef.current = pairId;
  // Верхний оверлей редактора (справка, меню, экспорт, комментарий).
  const backGuardRef = useRef<BackGuard | null>(null);
  // Задача, из строки которой открыли редактор: после возврата к списку
  // фокус должен вернуться туда же.
  const returnFocusPairIdRef = useRef<string | null>(null);

  // Перезагрузка в редакторе оставляет в history «редактор», хотя экран
  // начинает со списка. Нормализуем запись, иначе первый «Назад» из
  // списка выглядит как переход в редактор.
  useEffect(() => {
    const st = window.history.state as { vc?: string } | null;
    if (st?.vc !== "list") {
      window.history.replaceState({ vc: "list" }, "");
    }
  }, []);

  const closeEditor = useCallback(() => {
    setScreen("list");
    setPairId(null);
    reload();
  }, [reload]);

  const openEditor = useCallback((id: string) => {
    // Строку, из которой открыли задачу, вернём фокусом после «Назад»:
    // иначе клавиатурный пользователь теряет место в списке.
    returnFocusPairIdRef.current = id;
    setPairId(id);
    setScreen("editor");
    const st = window.history.state as { vc?: string } | null;
    if (st?.vc === "editor") {
      // Уже в редакторе: не плодим записи, иначе «Назад» будет
      // перебирать задачи вместо выхода к списку.
      window.history.replaceState({ vc: "editor", pairId: id }, "");
    } else {
      window.history.pushState({ vc: "editor", pairId: id }, "");
    }
  }, []);

  useEffect(() => {
    const onPop = (e: PopStateEvent) => {
      const st = e.state as { vc?: string; pairId?: string } | null;
      // Уходим из редактора, но открыт оверлей — закрываем его и
      // возвращаем запись редактора, чтобы следующий «Назад» уже вышел
      // к списку.
      if (screenRef.current === "editor" && st?.vc !== "editor") {
        if (backGuardRef.current?.()) {
          window.history.pushState(
            { vc: "editor", pairId: pairIdRef.current },
            "",
          );
          return;
        }
      }
      if (st?.vc === "editor" && st.pairId) {
        setPairId(st.pairId);
        setScreen("editor");
      } else {
        closeEditor();
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [closeEditor]);

  // Возврат к списку: фокус — на строку, из которой открыли редактор.
  // Строка пересоздаётся при смене экрана, поэтому ищем её по id задачи,
  // а не держим ссылку на размонтированный узел.
  useEffect(() => {
    if (screen !== "list") return;
    const id = returnFocusPairIdRef.current;
    if (id === null) return;
    returnFocusPairIdRef.current = null;
    const row = [...document.querySelectorAll<HTMLButtonElement>(".pair-open")].find(
      (button) => button.dataset.pairId === id,
    );
    row?.focus();
  }, [screen]);

  // Зеркало списка для тика: слияние лёгкого статуса без лишних рендеров.
  const pairsRef = useRef<VideoPair[]>([]);
  pairsRef.current = pairs;

  // Тик: волатильное состояние мержим в нарисованные строки; полный список —
  // только при смене состава (появились/ушли задачи), чужой правке разметки
  // (mtime) или переходе готовности (дотянуть метрики): статичное больше не
  // перечитывается и не гоняется по сети каждые 2 сек.
  const handleStatusTick = useCallback(async () => {
    let st;
    try {
      st = await getWorkspacesStatus();
    } catch {
      return; // следующий тик; явные действия и так делают полный reload
    }
    const prev = pairsRef.current;
    const byId = new Map(prev.map((p) => [p.id, p]));
    const structural =
      st.length !== prev.length || st.some((s) => !byId.has(s.id));
    const touched =
      !structural &&
      st.some((s) => {
        const p = byId.get(s.id);
        if (!p) return false;
        return (
          p.updated_at !== s.updated_at ||
          p.broken !== s.broken ||
          (p.indexing && !s.indexing)
        );
      });
    if (structural || touched) {
      reload();
      return;
    }
    const sm = new Map(st.map((s) => [s.id, s]));
    setPairs(prev.map((p) => ({ ...p, ...sm.get(p.id)! })));
  }, [reload]);

  useEffect(() => {
    authStatus()
      .then((s) =>
        setGate({
          checked: true,
          enabled: s.enabled,
          authed: !s.enabled || !!getAuthToken(),
        }),
      )
      .catch(() =>
        setGate({ checked: true, enabled: false, authed: true }),
      );
    // Любой 401 (протух/сменили токен) — назад на дверь.
    setUnauthorizedHandler(() => {
      clearAuthToken();
      setGate((g) => ({ ...g, authed: false }));
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    if (gate.checked && (!gate.enabled || gate.authed)) reload();
  }, [gate, reload]);

  const logout = useCallback(() => {
    clearAuthToken();
    setGate((g) => ({ ...g, authed: false }));
    setScreen("list");
    // Выход — это не «назад»: запись редактора больше не должна открываться
    // кнопкой браузера после повторного входа.
    window.history.replaceState({ vc: "list" }, "");
  }, []);

  // Заливки переживают смену экранов: форма только стартует, строки
  // живут в списке, ведро отменяет. Автооткрытия нет осознанно.
  const {
    uploads,
    startUpload,
    cancelUpload,
    dismissUpload,
    renameUpload,
    updateJob,
    removeJob,
  } = useUploadManager(reload);

  let content: ReactElement;
  if (!gate.checked) {
    content = (
      <div className="loading" role="status">
        Загрузка…
      </div>
    );
  } else if (gate.enabled && !gate.authed) {
    content = (
      <LoginScreen
        onOk={() => setGate((g) => ({ ...g, authed: true }))}
      />
    );
  } else if (screen === "editor" && pairId !== null) {
    content = (
      <CutEditor
        pairId={pairId}
        backGuardRef={backGuardRef}
        onBack={() => {
          // Возврат к списку идёт через history, чтобы не оставлять в ней
          // «редактор» позади списка. После правок reload обновляет
          // статус-бар и порядок «недавние сверху».
          if (window.history.state?.vc === "editor") {
            window.history.back();
          } else {
            closeEditor();
          }
        }}
      />
    );
  } else {
    content = (
      <PairList
        pairs={pairs}
        onSelect={openEditor}
        onChanged={reload}
        onTick={handleStatusTick}
        uploads={uploads}
        onStartUpload={startUpload}
        onCancelUpload={cancelUpload}
        onDismissUpload={dismissUpload}
        onRenameUpload={renameUpload}
        onUpdateJob={updateJob}
        onRemoveJob={removeJob}
        onLogout={gate.enabled ? logout : null}
      />
    );
  }

  return (
    <>
      <a className="skip-link" href="#main">
        Перейти к содержимому
      </a>
      <main id="main" className="app-main" tabIndex={-1}>
        {content}
      </main>
    </>
  );
}
