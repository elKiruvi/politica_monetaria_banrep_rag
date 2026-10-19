// DOM mínimo para ejecutar app.js en Node sin navegador ni dependencias.
import { readFileSync } from "node:fs";
import vm from "node:vm";

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.text = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
}
const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => Object.assign(new Node("#text"), { text: String(text) }),
  addEventListener: () => {},
};
const context = { document, window: { location: { origin: "http://127.0.0.1:8000" } }, URL, Node };
vm.createContext(context);
vm.runInContext(readFileSync(process.argv[2], "utf8") + "\nthis.renderAnswer = renderAnswer;", context);
const out = new Node("div");
context.renderAnswer(out, {
  status: "answered", text: "<b>4,7 %</b> [E1]", citations: [], evidence: [], warnings: [],
  corpus: {}, reason: null, added_citations: [], disclaimer: "Proyecto no oficial",
});
const text = out.textContent;
const html = JSON.stringify(out);
if (text.includes("null") || text.includes("undefined")) throw new Error(`texto nulo: ${text}`);
if (!text.includes("<b>4,7 %</b> [E1]")) throw new Error("el texto no se insertó como texto");
if (html.includes('"tag":"b"')) throw new Error("se creó un elemento desde datos");
console.log("ok");
