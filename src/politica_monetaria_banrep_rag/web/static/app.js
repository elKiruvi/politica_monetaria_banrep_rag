// Interfaz local sobre /api/v1. Todo texto proveniente de los informes o del modelo se inserta
// con textContent (nunca como HTML): el contenido de los PDF es dato no confiable.
"use strict";

const API = "/api/v1";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "href") node.href = safeUrl(value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fill(container, ...nodes) {
  // replaceChildren convierte null en el texto «null»: se descartan los nodos vacíos.
  container.replaceChildren(...nodes.filter((node) => node !== null && node !== undefined));
}

function safeUrl(url) {
  // Solo enlaces http(s) a fuentes; cualquier otro esquema se anula.
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "#";
  } catch {
    return "#";
  }
}

async function call(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const response = await fetch(API + path, options);
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = { detail: "Respuesta no válida del servidor." };
  }
  if (!response.ok) {
    const error = new Error(data.detail || `Error ${response.status}`);
    error.warnings = data.warnings || [];
    throw error;
  }
  return data;
}

function clip(text, size) {
  return text.length > size ? `${text.slice(0, size)}…` : text;
}

function pages(item) {
  const physical = item.page_start === item.page_end
    ? `pág. ${item.page_start}` : `págs. ${item.page_start}-${item.page_end}`;
  const printed = item.page_labels && item.page_labels.length
    ? ` (impresa ${item.page_labels.join(", ")})` : "";
  return `${physical} del PDF${printed}`;
}

function busy(container, message) {
  fill(container, el("p", { class: "busy", role: "status" }, message));
}

function showError(container, error) {
  // Los avisos (p. ej., de privacidad) se muestran aunque la operación falle.
  fill(container, warnings(error.warnings), el("p", { class: "error", role: "alert" }, error.message));
}

function warnings(list) {
  if (!list || !list.length) return null;
  return el("ul", { class: "warnings" }, ...list.map((w) => el("li", {}, w)));
}

function sourceList(citations, title) {
  if (!citations.length) return null;
  return el("div", { class: "sources" }, el("h3", {}, title), el("ol", {},
    ...citations.map((c) => el("li", {},
      el("strong", {}, `[${c.label}] `),
      `${c.report_title} (${c.reference_period}), ${pages(c)}; ${c.evidence_quality}. `,
      c.source_url ? el("a", { href: c.source_url, rel: "noopener noreferrer", target: "_blank" }, "Fuente") : "Sin URL verificada",
    ))));
}

const STATUS_TITLES = {
  answered: "Respuesta verificada",
  insufficient_evidence: "Sin respuesta: evidencia insuficiente",
  rejected: "Respuesta rechazada por la verificación",
};

function renderAnswer(container, data) {
  const shown = data.citations.length ? data.citations
    : (data.status !== "answered" ? data.evidence : []);
  fill(container,
    warnings([...(data.warnings || []), ...((data.corpus && data.corpus.warnings) || [])]),
    el("h3", { class: `status-${data.status}` }, STATUS_TITLES[data.status] || data.status),
    el("p", { class: "answer" }, data.text),
    data.reason ? el("p", { class: "reason" }, `Motivo: ${data.reason}`) : null,
    data.added_citations && data.added_citations.length
      ? el("p", { class: "reason" }, `Fuentes añadidas por la verificación: ${data.added_citations.join(", ")}`)
      : null,
    sourceList(shown, data.citations.length ? "Fuentes citadas" : "Evidencia revisada"),
    el("p", { class: "disclaimer" }, data.disclaimer || ""),
  );
}

const KINDS = {
  revision_de_proyeccion: "revisión de proyección",
  proyeccion_vs_observado: "proyección frente al dato observado",
  observado_vs_proyeccion: "dato observado frente a proyección",
  observados: "entre cifras no marcadas como proyección",
};

function number(value, signed) {
  const text = String(value).replace(".", ",");
  return signed && !text.startsWith("-") && /[1-9]/.test(text) ? `+${text}` : text;
}

function renderComparison(container, data) {
  const blocks = [
    warnings([
      ...(data.warnings || []),
      ...((data.missing_reports || []).length ? [`Informes no indexados: ${data.missing_reports.join(", ")}.`] : []),
      ...(data.corpus.warnings || []),
    ]),
    el("p", {}, `Indicador: «${data.indicator}»; filas buscadas con: ${(data.interpretation || []).join(" + ")}. Las cifras provienen de celdas atribuidas y las diferencias se calculan en Python.`),
  ];
  let table = null;
  for (const row of data.rows) {
    if (row.table !== table) {
      table = row.table;
      blocks.push(el("h3", {}, row.table));
    }
    const cells = row.values.map((v, i) => v
      ? el("li", {}, `${v.reference_period}: ${v.value_text} (${v.projection ? "proyección o supuesto" : "no marcada como proyección"}) — ${pages(v)}`)
      : el("li", { class: "note" }, `${(data.reports[i] || {}).reference_period || "Informe"}: sin dato en este cuadro.`));
    const diffs = row.differences.map((d) => el("li", { class: "difference" },
      `${d.from_report} → ${d.to_report}: ${number(d.value, true)} ${row.difference_unit} (${KINDS[d.kind] || d.kind})`));
    blocks.push(el("article", { class: "comparison" },
      el("h4", {}, `${row.indicator} — horizonte ${row.horizon}${row.unit ? ` (${row.unit})` : ""}`),
      el("ul", {}, ...cells),
      diffs.length ? el("ul", {}, ...diffs) : el("p", { class: "reason" }, "No comparable."),
      ...row.notes.map((n) => el("p", { class: "note" }, `Nota: ${n}`)),
    ));
  }
  if (data.truncated) blocks.push(el("p", { class: "reason" }, "Se muestran las primeras filas; precise el horizonte."));
  blocks.push(el("p", { class: "disclaimer" }, data.disclaimer));
  fill(container, ...blocks);
}

function renderSearch(container, data) {
  const items = data.results.map((r) => el("article", { class: "hit" },
    el("h3", {}, `${r.rank}. ${r.report.title} — ${r.report.reference_period}, ${pages(r)}`),
    el("p", { class: "meta" }, `Sección: ${r.section.join(" > ") || "(sin sección)"} · tipo: ${r.kind} · calidad: ${r.evidence_quality} · celdas atribuibles: ${r.attributed_cells.length}`),
    el("p", { class: "excerpt" }, r.text.length > 700 ? `${r.text.slice(0, 700)}…` : r.text),
    r.kind === "table_raw" ? el("p", { class: "note" }, "Cuadro sin estructura verificable: sus cifras no están atribuidas.") : null,
    r.table_notes.length ? el("p", { class: "note" }, `Notas del cuadro: ${clip(r.table_notes[0], 300)}`) : null,
  ));
  fill(container,
    warnings([...(data.warnings || []), ...(data.corpus.warnings || [])]),
    el("p", {}, `Modo: ${data.mode}.`),
    ...(items.length ? items : [el("p", {}, "Sin resultados para esa consulta.")]),
  );
}

async function loadStatus() {
  try {
    const status = await call("/status");
    const index = status.index;
    document.getElementById("estado-indice").textContent = index.available
      ? `${index.chunk_count} fragmentos; vectores: ${index.embedding_model || "no (solo búsqueda léxica)"}; creado ${index.created_at}`
      : `No disponible: ${index.error}`;
    const corpus = status.corpus || {};
    document.getElementById("estado-corpus").textContent = corpus.selection_mode === "local_import"
      ? "Importación local (no equivale a los últimos informes publicados)"
      : (corpus.selection_mode ? "Últimos informes sincronizados" : "—");
    const ollama = status.ollama;
    document.getElementById("estado-ollama").textContent = ollama.available
      ? `Disponible; modelo de generación ${status.generation_model} ${status.generation_model_installed ? "instalado" : "NO instalado"}`
      : "No disponible: preguntar y resumir no funcionarán; la búsqueda léxica y las comparaciones sí.";
    const list = document.getElementById("estado-avisos");
    list.replaceChildren(...[...(status.warnings || []), ...(corpus.warnings || [])].map((w) => el("li", {}, w)));
  } catch (error) {
    document.getElementById("estado-indice").textContent = `Error: ${error.message}`;
  }
  try {
    const { reports } = await call("/reports");
    const body = document.querySelector("#tabla-informes tbody");
    body.replaceChildren(...reports.map((r) => el("tr", {},
      el("td", {}, r.reference_period), el("td", {}, r.title),
      el("td", {}, r.published_at || "—"), el("td", {}, r.verification_status || "—"))));
    for (const id of ["comparar-informes", "resumir-informe"]) {
      document.getElementById(id).replaceChildren(...reports.map((r) =>
        el("option", { value: r.reference_period }, `${r.reference_period} — ${r.title}`)));
    }
  } catch {
    // El estado ya informa que no hay índice.
  }
}

function monthValue(input) {
  return input.value ? input.value : null;
}

function setupTabs() {
  // Patrón de pestañas de ARIA: una sola en el orden de tabulación; flechas, Inicio y Fin.
  const buttons = Array.from(document.querySelectorAll('[role="tab"]'));
  const select = (button) => {
    for (const other of buttons) {
      const selected = other === button;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
    }
    button.focus();
  };
  buttons.forEach((button, index) => {
    button.tabIndex = index === 0 ? 0 : -1;
    button.addEventListener("click", () => select(button));
    button.addEventListener("keydown", (event) => {
      const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: buttons.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      select(buttons[(moves[event.key] + buttons.length) % buttons.length]);
    });
  });
}

function submitter(form, out, message, request, render) {
  // Una solicitud a la vez por formulario; una respuesta obsoleta nunca reemplaza a otra.
  let sequence = 0;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = ++sequence;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    busy(out, message);
    try {
      const data = await request();
      if (current === sequence) render(out, data);
    } catch (error) {
      if (current === sequence) showError(out, error);
    } finally {
      if (current === sequence) button.disabled = false;
    }
  });
}

function setupForms() {
  const ask = document.getElementById("form-preguntar");
  submitter(ask, document.getElementById("resultado-preguntar"),
    "Recuperando evidencia y redactando con el modelo local… (puede tardar)", () => {
      const filters = {};
      const from = monthValue(ask.elements.period_from);
      const to = monthValue(ask.elements.period_to);
      if (from) filters.period_from = from;
      if (to) filters.period_to = to;
      return call("/query", { question: ask.elements.question.value, filters });
    }, renderAnswer);
  for (const suggestion of document.querySelectorAll(".suggestion")) {
    suggestion.addEventListener("click", () => {
      ask.elements.question.value = suggestion.textContent;
      ask.requestSubmit();
    });
  }

  const compareForm = document.getElementById("form-comparar");
  submitter(compareForm, document.getElementById("resultado-comparar"),
    "Comparando celdas atribuidas…", () => {
      const f = compareForm.elements;
      return call("/compare", {
        question: f.question.value || null,
        reports: Array.from(f.reports.selectedOptions).map((o) => o.value),
        indicator: f.indicator.value || null,
        horizon: f.horizon.value || null,
        include_observed: f.include_observed.checked,
      });
    }, renderComparison);

  const summary = document.getElementById("form-resumir");
  submitter(summary, document.getElementById("resultado-resumir"),
    "Resumiendo con el modelo local… (puede tardar)", () => call("/summarize", {
      report: summary.elements.report.value,
      topic: summary.elements.topic.value || null,
    }), renderAnswer);

  const search = document.getElementById("form-buscar");
  submitter(search, document.getElementById("resultado-buscar"), "Buscando…", () =>
    call("/search", {
      query: search.elements.query.value,
      top_k: Number(search.elements.top_k.value) || 5,
    }), renderSearch);
}

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupForms();
  loadStatus();
});
