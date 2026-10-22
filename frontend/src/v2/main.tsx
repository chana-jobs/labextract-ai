import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import AppV2 from "./AppV2";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Root element #root not found");

createRoot(rootEl).render(
  <StrictMode>
    <AppV2 />
  </StrictMode>
);
