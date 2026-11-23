import { useEffect } from "react";
import { ShortcutsHelp, Sidebar, Toasts, TopBar } from "./components/Layout";
import { BloomPage } from "./pages/BloomPage";
import { ChatPage } from "./pages/ChatPage";
import { ImpactAnalysis } from "./pages/ImpactAnalysis";
import { LineageExplorer } from "./pages/LineageExplorer";
import { PiiTracker } from "./pages/PiiTracker";
import { Pipelines } from "./pages/Pipelines";
import { QueryPage } from "./pages/QueryPage";
import { SchemaPage } from "./pages/SchemaPage";
import { useApp } from "./store/app";

export default function App() {
  const page = useApp((s) => s.page);
  const pageParams = useApp((s) => s.pageParams);
  const syncFromHash = useApp((s) => s.syncFromHash);

  useEffect(() => {
    window.addEventListener("popstate", syncFromHash);
    window.addEventListener("hashchange", syncFromHash);
    return () => {
      window.removeEventListener("popstate", syncFromHash);
      window.removeEventListener("hashchange", syncFromHash);
    };
  }, [syncFromHash]);

  // Lineage pages re-mount when their parameters change so local state (direction, depth) follows the URL.
  const key = page === "query" || page === "bloom" || page === "chat" ? page : `${page}:${JSON.stringify(pageParams)}`;

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-ink-950 text-ink-100">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="min-h-0 flex-1">
          {/* The Query page stays mounted so the editor and frames keep their state. */}
          <div className={page === "query" ? "h-full" : "hidden"}>
            <QueryPage />
          </div>
          {/* Bloom also stays mounted so the scene keeps its layout when switching pages. */}
          <div className={page === "bloom" ? "h-full" : "hidden"}>
            <BloomPage />
          </div>
          {/* Chat stays mounted too, so the conversation survives switching pages. */}
          <div className={page === "chat" ? "h-full" : "hidden"}>
            <ChatPage />
          </div>
          {page !== "query" && page !== "bloom" && page !== "chat" && (
            <div key={key} className="h-full">
              {page === "lineage" && <LineageExplorer />}
              {page === "impact" && <ImpactAnalysis />}
              {page === "pii" && <PiiTracker />}
              {page === "pipelines" && <Pipelines />}
              {page === "schema" && <SchemaPage />}
            </div>
          )}
        </main>
      </div>
      <Toasts />
      <ShortcutsHelp />
    </div>
  );
}
