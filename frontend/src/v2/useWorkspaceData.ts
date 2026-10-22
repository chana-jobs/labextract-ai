/**
 * Loads every document of the workspace and its drafts through the existing API.
 * QUEUED/RUNNING documents are NOT processed automatically: they stay "pending"
 * until the user explicitly starts processing (POST drafts/init).
 * One document failing never hides the others.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  getAuthSessionGeneration,
  getDocument,
  getDrafts,
  isAuthSessionCurrent,
  listDocuments,
  processDocument,
} from "../api/client";
import type { DocumentMeta } from "../types/contracts";
import type { DocEntry, DocLoad } from "./model";
import { errorMessage, isAbort } from "./errors";

type ListState = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };

async function loadDrafts(meta: DocumentMeta): Promise<DocLoad> {
  if (meta.processing_status === "QUEUED" || meta.processing_status === "RUNNING") return { state: "pending" };
  try {
    const result = await getDrafts(meta.file_id);
    return { state: "ready", forms: result.forms ?? [result.form] };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return { state: "pending" };
    if (err instanceof ApiError && err.status === 422) return { state: "error", message: "השרת לא החזיר טפסים עבור קובץ זה." };
    throw err;
  }
}

export function useWorkspaceData() {
  const generation = useRef(getAuthSessionGeneration());
  const mounted = useRef(true);
  const [list, setList] = useState<ListState>({ state: "loading" });
  const [docs, setDocs] = useState<DocEntry[]>([]);

  const live = useCallback(() => mounted.current && isAuthSessionCurrent(generation.current), []);

  const setLoad = useCallback((fileId: string, load: DocLoad, meta?: DocumentMeta) => {
    setDocs((prev) => prev.map((d) => (d.meta.file_id === fileId ? { meta: meta ?? d.meta, load } : d)));
  }, []);

  const fetchDoc = useCallback(async (meta: DocumentMeta) => {
    try {
      const load = await loadDrafts(meta);
      if (live()) setLoad(meta.file_id, load);
    } catch (err) {
      if (live() && !isAbort(err)) setLoad(meta.file_id, { state: "error", message: errorMessage(err) });
    }
  }, [live, setLoad]);

  const reload = useCallback(async () => {
    setList({ state: "loading" });
    try {
      const metas = await listDocuments();
      if (!live()) return;
      setDocs(metas.map((meta) => ({ meta, load: { state: "loading" } })));
      setList({ state: "ready" });
      await Promise.all(metas.map(fetchDoc));
    } catch (err) {
      if (live() && !isAbort(err)) setList({ state: "error", message: errorMessage(err) });
    }
  }, [fetchDoc, live]);

  useEffect(() => {
    mounted.current = true;
    void reload();
    return () => { mounted.current = false; };
  }, [reload]);

  /** Re-read one document's drafts from the server (after save/approve/conflict). Throws on failure. */
  const refreshDoc = useCallback(async (fileId: string) => {
    const result = await getDrafts(fileId);
    if (live()) setLoad(fileId, { state: "ready", forms: result.forms ?? [result.form] });
  }, [live, setLoad]);

  /** Explicit user action: start (or resume) extraction for a pending document, one page per request. */
  const processDoc = useCallback(async (fileId: string) => {
    setLoad(fileId, { state: "processing" });
    try {
      const current = await getDocument(fileId);
      await processDocument(fileId, current.page_count ?? 1, (done, total) => {
        if (live()) setLoad(fileId, { state: "processing", done, total });
      });
      const [meta, result] = await Promise.all([getDocument(fileId), getDrafts(fileId)]);
      if (live()) setLoad(fileId, { state: "ready", forms: result.forms ?? [result.form] }, meta);
    } catch (err) {
      if (live() && !isAbort(err)) setLoad(fileId, { state: "error", message: errorMessage(err) });
    }
  }, [live, setLoad]);

  const addDoc = useCallback((meta: DocumentMeta) => {
    setDocs((prev) => [{ meta, load: { state: "loading" } }, ...prev.filter((d) => d.meta.file_id !== meta.file_id)]);
    void fetchDoc(meta);
  }, [fetchDoc]);

  return { list, docs, reload, refreshDoc, processDoc, addDoc, live };
}
