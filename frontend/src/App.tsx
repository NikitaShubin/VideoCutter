import { useCallback, useEffect, useState } from "react";
import { listPairs } from "./api";
import { PairList } from "./components/PairList";
import { CutEditor } from "./components/CutEditor";
import { useUploadManager } from "./model/uploads";
import type { VideoPair } from "./types";

type Screen = "list" | "editor";

export function App() {
  const [screen, setScreen] = useState<Screen>("list");
  const [pairs, setPairs] = useState<VideoPair[]>([]);
  const [pairId, setPairId] = useState<string | null>(null);

  const reload = useCallback(() => {
    listPairs().then(setPairs).catch(console.error);
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

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
    />
  );
}
