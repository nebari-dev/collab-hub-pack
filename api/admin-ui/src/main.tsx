import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("index.html must carry the #root element the panel mounts into");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
