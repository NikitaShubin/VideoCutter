import { useCallback, useEffect, useState } from "react";
import { listPairs } from "./api";
import { PairList } from "./components/PairList";
import { CutEditor } from "./components/CutEditor";
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
    />
  );
}
