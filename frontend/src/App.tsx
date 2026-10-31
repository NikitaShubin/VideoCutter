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
import { CutEditor } from "./components/CutEditor";
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
        onBack={() => {
          setScreen("list");
          reload(); // после правок обновляем статус-бар и порядок «недавние сверху»
        }}
      />
    );
  } else {
    content = (
      <PairList
        pairs={pairs}
        onSelect={(id) => {
          setPairId(id);
          setScreen("editor");
        }}
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
