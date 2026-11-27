// Reads a JSON array of Mermaid sources on stdin, prints a JSON array of {ok, error?} (one per source).
// Mermaid's parser wants a DOM, so a jsdom window stands in for the browser.
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
try {
  globalThis.navigator ??= dom.window.navigator;
} catch {
  /* Node 21+ defines a read-only navigator: fine */
}
const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

let input = "";
for await (const chunk of process.stdin) input += chunk;
const out = [];
for (const src of JSON.parse(input)) {
  try {
    await mermaid.parse(src);
    out.push({ ok: true });
  } catch (e) {
    out.push({ ok: false, error: String((e && (e.str || e.message)) || e) });
  }
}
console.log(JSON.stringify(out));
