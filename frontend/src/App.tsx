import { useEffect } from "react";
import { ChatLauncher, ShortcutsHelp, Sidebar, Toasts, TopBar } from "./components/Layout";
import { BloomPage } from "./pages/BloomPage";
import { ChatPage } from "./pages/ChatPage";
import { ImpactAnalysis } from "./pages/ImpactAnalysis";
import { JobsPage } from "./pages/JobsPage";
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

  const chatOpen = useApp((s) => s.chatOpen);
  const setChatOpen = useApp((s) => s.setChatOpen);
  const showShortcuts = useApp((s) => s.showShortcuts);

  // Esc closes the chat (unless the shortcuts help is on top: that one takes the key first).
  useEffect(() => {
    if (!chatOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !showShortcuts) setChatOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chatOpen, showShortcuts, setChatOpen]);

  // Lineage pages re-mount when their parameters change so local state (direction, depth) follows the URL.
  const key = page === "query" || page === "bloom" ? page : `${page}:${JSON.stringify(pageParams)}`;

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
          {page !== "query" && page !== "bloom" && (
            <div key={key} className="h-full">
              {page === "jobs" && <JobsPage />}
              {page === "lineage" && <LineageExplorer />}
              {page === "impact" && <ImpactAnalysis />}
              {page === "pii" && <PiiTracker />}
              {page === "pipelines" && <Pipelines />}
              {page === "schema" && <SchemaPage />}
            </div>
          )}
        </main>
      </div>
      {/* The chat: always mounted so the conversation survives closing it, full screen while open. */}
      <div
        className={chatOpen ? "fixed inset-0 z-[80] bg-ink-950" : "hidden"}
        role="dialog"
        aria-modal={chatOpen}
        aria-label="AI chat"
        aria-hidden={!chatOpen}
      >
        <ChatPage />
      </div>
      <ChatLauncher />
      <Toasts />
      <ShortcutsHelp />
    </div>
  );
}
