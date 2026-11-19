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
import {
  listPath,
  parseRoute,
  projectFilterPath,
  projectPath,
  taskPath,
  withFrame,
  PROJECT_ALL,
  type RouteState,
  type Tab,
} from "./model/routes";
import type { VideoPair } from "./types";

interface Gate {
  checked: boolean;
  enabled: boolean;
  authed: boolean;
}

/** Адрес из окна, разобранный в состояние экрана. */
function currentRoute(): RouteState {
  return parseRoute(window.location.pathname, window.location.search);
}

export function App() {
  // Адрес — источник правды: вкладка, фильтр, задача и кадр выводятся из
  // pathname + search. history.state остаётся вспомогательным (vc/pairId):
  // он нужен только заставке «Назад» и совместимости со старыми записями.
  const [route, setRoute] = useState<RouteState>(currentRoute);
  const [pairs, setPairs] = useState<VideoPair[]>([]);
  const [pairsChecked, setPairsChecked] = useState(false);
  // Входная дверь: закрыта — экран токена вместо списка.
  const [gate, setGate] = useState<Gate>({
    checked: false,
    enabled: false,
    authed: false,
  });

  // Перечитывание списка: отдаём свежие задачи — вызывающий (кнопка
  // синхронизации) сравнивает их с прежним снимком и рассказывает, что
  // изменилось на диске.
  const reload = useCallback(async () => {
    try {
      const items = await listPairs();
      setPairs(items);
      return items;
    } finally {
      // Даже сбой списка снимает «ещё не проверено»: deep link на задачу
      // не должен навсегда застрять в ожидании проверки реестра.
      setPairsChecked(true);
    }
  }, []);

  // Прежние вызовы не ждут результата: обрыв сети не должен ронять экран.
  const reloadQuiet = useCallback(() => {
    void reload().catch(console.error);
  }, [reload]);

  // Свежие route/pairId нужны обработчику popstate без пересоздания
  // подписки на каждое изменение.
  const routeRef = useRef<RouteState>(route);
  routeRef.current = route;
  // Верхний оверлей редактора (справка, меню, экспорт, комментарий).
  const backGuardRef = useRef<BackGuard | null>(null);
  // Задача, из строки которой открыли редактор: после возврата к списку
  // фокус должен вернуться туда же.
  const returnFocusPairIdRef = useRef<string | null>(null);

  // Начальная запись истории приводится в соответствие адресу.
  // Deep link на задачу получает запись списка позади себя: «Назад» из
  // редактора возвращает к задачам, а не выбрасывает на чужой сайт.
  useEffect(() => {
    const r = currentRoute();
    // Адрес снимаем ДО replaceState: иначе ?frame= и путь редактора
    // потерялись бы вместе с первой записью.
    const url = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const st = window.history.state as { vc?: string; pairId?: string } | null;
    if (r.screen === "editor" && r.pairId !== null) {
      if (st?.vc !== "editor" || st.pairId !== r.pairId) {
        window.history.replaceState({ vc: "list" }, "", "/tasks");
        window.history.pushState(
          { vc: "editor", pairId: r.pairId },
          "",
          url,
        );
      }
    } else if (st?.vc !== "list") {
      // Перезагрузка старой вкладки могла оставить state «редактор», хотя
      // адрес уже список: нормализуем запись, иначе первый «Назад» из
      // списка выглядит как переход в редактор.
      window.history.replaceState({ vc: "list" }, "");
    }
  }, []);

  const leaveEditor = useCallback(() => {
    // Прямой вход без записи списка позади: заменяем адрес редактора на
    // список, чтобы в истории не осталось «мёртвой» задачи.
    window.history.replaceState({ vc: "list" }, "", "/tasks");
    setRoute(currentRoute());
    reloadQuiet();
  }, [reloadQuiet]);

  const openEditor = useCallback((id: string) => {
    // Строку, из которой открыли задачу, вернём фокусом после «Назад»:
    // иначе клавиатурный пользователь теряет место в списке.
    returnFocusPairIdRef.current = id;
    const path = taskPath(id);
    if (routeRef.current.screen === "editor") {
      // Уже в редакторе: не плодим записи, иначе «Назад» будет
      // перебирать задачи вместо выхода к списку.
      window.history.replaceState({ vc: "editor", pairId: id }, "", path);
    } else {
      window.history.pushState({ vc: "editor", pairId: id }, "", path);
    }
    setRoute(currentRoute());
  }, []);

  // Переходы по списку (вкладки, проекты) — push: каждая осмысленная
  // навигация оставляет шаг назад. Повтор текущего адреса не дублируем.
  const goToList = useCallback((path: string) => {
    const current = `${window.location.pathname}${window.location.search}`;
    if (current === path) {
      setRoute(currentRoute());
      return;
    }
    window.history.pushState({ vc: "list" }, "", path);
    setRoute(currentRoute());
  }, []);

  const pickTab = useCallback(
    (tab: Tab) => {
      goToList(listPath(tab));
    },
    [goToList],
  );

  const openProject = useCallback(
    (id: string) => {
      goToList(projectPath(id));
    },
    [goToList],
  );

  const pickProjectFilter = useCallback(
    (filter: string) => {
      goToList(projectFilterPath(filter));
    },
    [goToList],
  );

  // Кадр в адресе обновляется заменой записи: «Назад» должен вести к
  // списку, а не листать кадры. Zero-based — как ?frame= в CVAT.
  const updateFrameInUrl = useCallback((frame: number) => {
    if (currentRoute().screen !== "editor") return;
    const path = withFrame(
      window.location.pathname,
      window.location.search,
      frame,
    );
    window.history.replaceState(
      window.history.state,
      "",
      `${path}${window.location.hash}`,
    );
  }, []);

  useEffect(() => {
    const onPop = () => {
      const next = currentRoute();
      const current = routeRef.current;
      // Уходим из редактора, но открыт оверлей — закрываем его и
      // возвращаем запись редактора, чтобы следующий «Назад» уже вышел
      // к списку.
      if (current.screen === "editor" && next.screen !== "editor") {
        if (backGuardRef.current?.()) {
          if (current.pairId !== null) {
            window.history.pushState(
              { vc: "editor", pairId: current.pairId },
              "",
              taskPath(current.pairId),
            );
          }
          return;
        }
      }
      setRoute(next);
      if (next.screen === "list") reloadQuiet();
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [reloadQuiet]);

  // Возврат к списку: фокус — на строку, из которой открыли редактор.
  // Строка пересоздаётся при смене экрана, поэтому ищем её по id задачи,
  // а не держим ссылку на размонтированный узел.
  useEffect(() => {
    if (route.screen !== "list") return;
    const id = returnFocusPairIdRef.current;
    if (id === null) return;
    returnFocusPairIdRef.current = null;
    const row = [...document.querySelectorAll<HTMLButtonElement>(".pair-open")].find(
      (button) => button.dataset.pairId === id,
    );
    row?.focus();
  }, [route.screen]);

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
      reloadQuiet();
      return;
    }
    const sm = new Map(st.map((s) => [s.id, s]));
    setPairs(prev.map((p) => ({ ...p, ...sm.get(p.id)! })));
  }, [reloadQuiet]);

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
    if (gate.checked && (!gate.enabled || gate.authed)) reloadQuiet();
  }, [gate, reloadQuiet]);

  const logout = useCallback(() => {
    clearAuthToken();
    setGate((g) => ({ ...g, authed: false }));
    // Выход — это не «назад»: запись редактора больше не должна открываться
    // кнопкой браузера после повторного входа.
    window.history.replaceState({ vc: "list" }, "", "/");
    setRoute(currentRoute());
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
  } = useUploadManager(reloadQuiet);

  // Deep link на несуществующую задачу: показываем заметку и список, но
  // адрес не переписываем — пользователь видит, что именно открывал.
  // Проверка ждёт первый ответ реестра, иначе на медленной сети любая
  // задача успела бы выглядеть «неизвестной».
  const editorId = route.screen === "editor" ? route.pairId : null;
  const missingPairId =
    editorId !== null &&
    pairsChecked &&
    !pairs.some((pair) => pair.id === editorId)
      ? editorId
      : null;

  useEffect(() => {
    if (missingPairId === null) return;
    // Запись редактора больше не действительна: следующий клик по задаче
    // должен push-ить, а не replace-ить несуществующий редактор.
    const st = window.history.state as { vc?: string } | null;
    if (st?.vc === "editor") {
      window.history.replaceState({ vc: "list" }, "");
    }
  }, [missingPairId]);

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
  } else if (editorId !== null && missingPairId === null) {
    content = (
      <CutEditor
        key={editorId}
        pairId={editorId}
        initialFrame={route.frame}
        initialFragment={route.fragment}
        onFrameChange={updateFrameInUrl}
        backGuardRef={backGuardRef}
        onBack={() => {
          // Возврат к списку идёт через history, чтобы не оставлять в ней
          // «редактор» позади списка. После правок reload обновляет
          // статус-бар и порядок «недавние сверху».
          if (window.history.state?.vc === "editor") {
            window.history.back();
          } else {
            leaveEditor();
          }
        }}
      />
    );
  } else {
    content = (
      <PairList
        pairs={pairs}
        onSelect={openEditor}
        onChanged={reloadQuiet}
        onTick={handleStatusTick}
        onReload={reload}
        uploads={uploads}
        onStartUpload={startUpload}
        onCancelUpload={cancelUpload}
        onDismissUpload={dismissUpload}
        onRenameUpload={renameUpload}
        onUpdateJob={updateJob}
        onRemoveJob={removeJob}
        onLogout={gate.enabled ? logout : null}
        tab={missingPairId !== null ? "tasks" : (route.tab ?? undefined)}
        projectFilter={
          missingPairId !== null
            ? PROJECT_ALL
            : route.tab === null
              ? undefined
              : (route.project ?? PROJECT_ALL)
        }
        onTabChange={pickTab}
        onProjectOpen={openProject}
        onProjectFilterChange={pickProjectFilter}
        notice={
          missingPairId !== null
            ? `Задача «${missingPairId}» не найдена — показан список задач.`
            : null
        }
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
