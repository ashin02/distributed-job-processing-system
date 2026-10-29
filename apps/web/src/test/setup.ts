import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

let generatedId = 0;

Object.defineProperty(globalThis.crypto, "randomUUID", {
  configurable: true,
  value: () => `00000000-0000-4000-8000-${String(++generatedId).padStart(12, "0")}`,
});

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});
