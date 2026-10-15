// TypeScript entry point for the same guard. pi 0.85.1 discovers extensions as `*.ts` and loads `-e
// <path>` through its own TS loader; that a plain `.js` path is accepted is NOT verified in the research
// report, so this two-line shim exists as the fallback. Point the adapter at it with
// `--set pi.guard_path=<abs path to this file>` if a pi build refuses the `.js`.
//
// All logic (and all comments explaining it) lives in harness-guard.js — keep this file a re-export.
// @ts-ignore — pi resolves the sibling .js at runtime; this file is never type-checked by us.
export { default, decide, loadPolicy, callPolicy, recordDenial, CANONICAL_TOOL } from "./harness-guard.js";
