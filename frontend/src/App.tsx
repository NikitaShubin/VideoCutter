import { useCallback, useEffect, useState } from "react";
import {
  authStatus,
  clearAuthToken,
  getAuthToken,
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

  if (!gate.checked) {
    return <div className="loading">Загрузка…</div>;
  }

  if (gate.enabled && !gate.authed) {
    return (
      <LoginScreen
        onOk={() => setGate((g) => ({ ...g, authed: true }))}
      />
    );
  }

  if (screen === "editor" && pairId !== null) {
    return (
      <CutEditor
        pairId={pairId}
        onBack={() => {
          setScreen("list");
          reload(); // после правок обновляем статус-бар и порядок «недавние сверху»
        }}
      />
    );
  }

  return (
    <PairList
      pairs={pairs}
      onSelect={(id) => {
        setPairId(id);
        setScreen("editor");
      }}
      onChanged={reload}
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
