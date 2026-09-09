import { useCallback, useEffect, useState } from "react";
import { listPairs } from "./api";
import { PairList } from "./components/PairList";
import { UploadForm } from "./components/UploadForm";
import { CutEditor } from "./components/CutEditor";
import type { VideoPair, VideoPairDetail } from "./types";

type Screen = "list" | "upload" | "editor";

export function App() {
  const [screen, setScreen] = useState<Screen>("list");
  const [pairs, setPairs] = useState<VideoPair[]>([]);
  const [pairId, setPairId] = useState<number | null>(null);

  const reload = useCallback(() => {
    listPairs().then(setPairs).catch(console.error);
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const onCreated = (pair: VideoPairDetail) => {
    reload();
    setPairId(pair.id);
    setScreen("editor");
  };

  if (screen === "editor" && pairId !== null) {
    return <CutEditor pairId={pairId} onBack={() => setScreen("list")} />;
  }

  if (screen === "upload") {
    return (
      <>
        <UploadForm onCreated={onCreated} />
        <button className="back" onClick={() => setScreen("list")}>← Назад</button>
      </>
    );
  }

  return (
    <PairList
      pairs={pairs}
      onSelect={(id) => {
        setPairId(id);
        setScreen("editor");
      }}
      onUpload={() => setScreen("upload")}
    />
  );
}