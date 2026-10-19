"""Reportes descargables (Markdown, HTML imprimible y PDF) de consultas, comparaciones y resúmenes.

Un reporte se construye a partir del resultado ya verificado; no vuelve a llamar al modelo. Incluye
título, fecha, informes usados y su vigencia, metodología, resultados con unidades y horizontes,
citas, advertencias, estado del corpus y la declaración de proyecto no oficial. Nunca incluye rutas
locales ni el contenido completo de los PDF.
"""

import html
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from politica_monetaria_banrep_rag.answering import Answer
from politica_monetaria_banrep_rag.comparison import Comparison
from politica_monetaria_banrep_rag.service import (
    DISCLAIMER,
    CompareResult,
    QueryResult,
    SummaryResult,
)

FORMATS = {"md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8",
           "pdf": "application/pdf"}  # fmt: skip
_STATUS = {
    "answered": "Respuesta verificada",
    "insufficient_evidence": "Sin respuesta: evidencia insuficiente",
    "rejected": "Respuesta rechazada por la verificación",
}
_KINDS = {
    "revision_de_proyeccion": "revisión de proyección",
    "proyeccion_vs_observado": "proyección frente al dato observado",
    "observado_vs_proyeccion": "dato observado frente a proyección",
    "observados": "entre cifras no marcadas como proyección",
}
_STATES = {
    "verified": "verificado en la fuente",
    "revalidation_pending": "revalidación pendiente",
    "unavailable": "no disponible para verificar",
    "local_import": "importación local (no verificada en la fuente)",
    "snapshot_outdated": "el catálogo activo está desactualizado",
    "fuera_del_catalogo": "fuera del catálogo activo",
}
_ACQUISITION = {"local_import": "importación local", "download": "descarga oficial"}
_MODES = {"hibrido": "híbrida", "lexico": "léxica", "semantico": "semántica"}
# Las fuentes estándar del PDF cubren cp1252; estos símbolos se transliteran solo en el PDF.
_PDF_TRANSLITERATION = str.maketrans({"→": "->", "−": "-", "‹": "<", "›": ">", "≥": ">=",
                                      "≤": "<=", "≈": "~"})  # fmt: skip


@dataclass(frozen=True)
class Section:
    title: str
    paragraphs: tuple[str, ...] = ()
    table: tuple[tuple[str, ...], ...] = ()  # la primera fila es el encabezado


@dataclass(frozen=True)
class Report:
    title: str
    kind: str
    created_at: str
    parameters: tuple[tuple[str, str], ...]
    sources: tuple[tuple[str, ...], ...]  # periodo, título, URL, adquisición, vigencia
    methodology: tuple[str, ...]
    sections: tuple[Section, ...]
    citations: tuple[str, ...]
    warnings: tuple[str, ...]
    corpus: tuple[str, ...]
    disclaimer: str = DISCLAIMER
    filename: str = field(default="reporte")
    # «Citas» solo si respaldan una respuesta publicada; si no, es la evidencia revisada.
    citations_title: str = "Citas"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _pages(start: int, end: int, labels: tuple[str, ...] | list[str]) -> str:
    physical = f"pág. {start}" if start == end else f"págs. {start}-{end}"
    printed = f" (impresa {', '.join(labels)})" if labels else ""
    return f"{physical} del PDF{printed}"


def _corpus(corpus: dict[str, object], index_id: str) -> tuple[tuple[str, ...], dict[str, str]]:
    mode = corpus.get("selection_mode")
    lines = [f"Índice {index_id[:12]}."]
    if mode == "local_import":
        lines.append("Corpus de importación local: no equivale a los últimos informes publicados.")
    elif mode:
        lines.append("Corpus sincronizado con la fuente (últimos informes).")
    if corpus.get("index_matches_catalog") is False:
        lines.append("El índice no corresponde al catálogo activo.")
    reports = corpus.get("reports")
    states = {
        str(item["report_id"]): _STATES.get(
            str(item.get("verification_status")), str(item.get("verification_status"))
        )
        for item in (reports if isinstance(reports, list) else [])
        if isinstance(item, dict)
    }
    return tuple(lines), states


def _corpus_warnings(corpus: dict[str, object]) -> list[str]:
    warnings = corpus.get("warnings")
    return [str(w) for w in warnings] if isinstance(warnings, list) else []


def _citations_title(answer: Answer) -> str:
    if answer.status == "answered":
        return "Citas"
    return "Evidencia revisada (no respalda ninguna respuesta publicada)"


def _acquisition(state: str | None) -> str:
    """Las citas no llevan el método de adquisición; se deduce del estado de vigencia."""
    if state is None:
        return "—"
    return "importación local" if state == _STATES["local_import"] else "descarga oficial"


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _answer_parts(answer: Answer) -> tuple[Section, tuple[str, ...], list[tuple[str, ...]]]:
    paragraphs = [_STATUS.get(answer.status, answer.status), answer.text]
    if answer.reason:
        paragraphs.append(f"Motivo: {answer.reason}")
    if answer.added_citations:
        paragraphs.append(
            "Fuentes añadidas por la verificación (respaldan cifras que el modelo no citó): "
            + ", ".join(answer.added_citations)
        )
    shown = answer.citations or (answer.evidence if answer.status != "answered" else ())
    citations = tuple(
        f"[{c.label}] {c.report_title} ({c.reference_period}), "
        f"{_pages(c.page_start, c.page_end, c.page_labels)}; calidad {c.evidence_quality}. "
        f"{c.source_url or 'Sin URL verificada.'}"
        for c in shown
    )
    used = {c.report_id: c for c in shown}
    sources: list[tuple[str, ...]] = [
        (c.report_id, c.reference_period, c.report_title, c.source_url or "—")
        for c in sorted(used.values(), key=lambda c: c.reference_period)
    ]
    return Section("Resultado", tuple(paragraphs)), citations, sources


_ANSWER_METHOD = (
    "Recuperación de fragmentos de los informes indexados (vía {mode}).",
    "Redacción con el modelo local {model} usando solo la evidencia recuperada, tratada como datos "
    "no confiables.",
    "Verificación antes de publicar: citas existentes y cada cifra presente en la evidencia "
    "{scope}; si falla, la respuesta no se muestra.",
    "Las cifras de cuadros sin estructura verificable y de gráficos no se atribuyen.",
)


def report_from_query(result: QueryResult, question: str) -> Report:
    section, citations, sources = _answer_parts(result.answer)
    corpus, states = _corpus(result.corpus, result.index_id)
    return Report(
        title="Consulta a los Informes de Política Monetaria",
        kind="consulta",
        created_at=_now(),
        parameters=(
            ("Pregunta", question),
            ("Informes detectados en la pregunta", ", ".join(result.detected_reports) or "ninguno"),
        ),  # fmt: skip
        sources=tuple(
            (*row[1:], _acquisition(states.get(row[0])), states.get(row[0], "—")) for row in sources
        ),
        methodology=tuple(
            line.format(
                mode=_MODES.get(result.mode, result.mode), model=result.model, scope="citada"
            )
            for line in _ANSWER_METHOD
        ),
        sections=(section,),
        citations=citations,
        warnings=_unique([*result.warnings, *_corpus_warnings(result.corpus)]),
        corpus=corpus,
        filename="consulta",
        citations_title=_citations_title(result.answer),
    )


def report_from_summary(result: SummaryResult) -> Report:
    section, citations, _ = _answer_parts(result.answer)
    corpus, states = _corpus(result.corpus, result.index_id)
    meta = result.report
    return Report(
        title=f"Resumen: {meta.title}",
        kind="resumen",
        created_at=_now(),
        parameters=(("Informe", f"{meta.reference_period} — {meta.title}"), ("Tema", result.topic)),
        sources=(
            (
                meta.reference_period,
                meta.title,
                meta.source_url or "—",
                _ACQUISITION.get(meta.acquisition_method, meta.acquisition_method),
                states.get(meta.report_id, "—"),
            ),
        ),  # fmt: skip
        methodology=tuple(
            line.format(
                mode=f"{_MODES.get(result.mode, result.mode)}, restringida al informe",
                model=result.model,
                scope="revisada del informe (si el modelo no citó la fuente, se agrega)",
            )
            for line in _ANSWER_METHOD
        ),  # fmt: skip
        sections=(section,),
        citations=citations,
        warnings=_unique([*result.warnings, *_corpus_warnings(result.corpus)]),
        corpus=corpus,
        filename=f"resumen-{meta.reference_period}",
        citations_title=_citations_title(result.answer),
    )


def _number(value: object, *, signed: bool = False) -> str:
    text = str(value).replace(".", ",")
    return f"+{text}" if signed and not text.startswith("-") and text.strip("0,") else text


def _comparison_sections(comparison: Comparison) -> tuple[Section, ...]:
    sections = []
    for row in comparison.rows:
        unit = f" ({row.unit})" if row.unit else ""
        table = [("Informe", "Valor", "Tipo", "Página")]
        for value in row.values:
            if value is None:
                continue
            table.append((
                value.reference_period,
                value.value_text,
                "proyección o supuesto" if value.projection else "no marcada como proyección",
                _pages(value.page_start, value.page_end, value.page_labels),
            ))  # fmt: skip
        paragraphs = [
            f"Diferencia {d.from_report} → {d.to_report}: {_number(d.value, signed=True)} "
            f"{row.difference_unit} ({_KINDS.get(d.kind, d.kind)})."
            for d in row.differences
        ]
        if not row.comparable:
            paragraphs.append("No comparable.")
        paragraphs += [f"Nota: {note}" for note in row.notes]
        sections.append(
            Section(
                f"{row.table} — {row.indicator} — horizonte {row.horizon}{unit}",
                tuple(paragraphs),
                tuple(table),
            )  # fmt: skip
        )
    return tuple(sections)


def report_from_comparison(result: CompareResult, parameters: dict[str, str | None]) -> Report:
    comparison = result.comparison
    corpus, states = _corpus(result.corpus, result.index_id)
    notes = tuple(
        f"Notas del cuadro ({tid[:8]}): {text}" for tid, text in result.table_notes.items()
    )
    sections = _comparison_sections(comparison)
    if not sections:
        sections = (Section("Resultado", ("No hay filas comparables.",)),)
    return Report(
        title=f"Comparación entre informes: {comparison.indicator}",
        kind="comparacion",
        created_at=_now(),
        parameters=tuple((k, v) for k, v in parameters.items() if v)
        + (("Filas buscadas con", " + ".join(comparison.interpretation)),),
        sources=tuple(
            (
                r.reference_period,
                r.title,
                r.source_url or "—",
                _ACQUISITION.get(r.acquisition_method, r.acquisition_method),
                states.get(r.report_id, "—"),
            )
            for r in comparison.reports
        ),  # fmt: skip
        methodology=(
            "Se usan solo celdas atribuidas de cuadros con estructura verificada (fila, columna y "
            "unidad); los cuadros sin estructura y los gráficos no aportan cifras.",
            "Solo se comparan celdas del mismo cuadro, fila, horizonte y unidad; filas ambiguas o "
            "con unidades distintas no se comparan.",
            "Las diferencias se calculan en Python con aritmética decimal (no con el modelo de "
            "lenguaje), en puntos porcentuales cuando la unidad es porcentual.",
            "Se distingue la revisión entre proyecciones de la comparación con datos observados.",
        ),
        sections=sections,
        citations=notes,
        warnings=_unique(
            [*result.warnings, *comparison.warnings, *_corpus_warnings(result.corpus)]
            + (
                [
                    "Se muestran solo las primeras filas: precise el horizonte o el cuadro para "
                    "ver el resto."
                ]
                if comparison.truncated
                else []
            )
        ),  # fmt: skip
        corpus=corpus,
        filename="comparacion",
    )


# --- renderizado --------------------------------------------------------------------------


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|!])")


def _md(text: str) -> str:
    """Una sola línea con el marcado de Markdown escapado: un texto del modelo o de los PDF no
    puede crear encabezados, enlaces ni HTML en el reporte."""
    escaped = _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))
    # Las marcas de cita «[E1]» no forman enlaces sin «(…)», que sí queda escapado.
    return re.sub(r"\\\[(E\d+(?:, ?E\d+)*)\\\]", r"[\1]", escaped)


def _md_cell(text: str) -> str:
    return _md(text)


def to_markdown(report: Report) -> str:
    lines = [
        f"# {_md(report.title)}",
        "",
        f"Generado: {report.created_at}. {report.disclaimer}",
        "",
    ]
    lines += ["## Parámetros", ""] + [f"- **{k}:** {_md(v)}" for k, v in report.parameters] + [""]
    lines += [
        "## Informes utilizados",
        "",
        "| Periodo | Informe | Fuente | Adquisición | Vigencia |",
        "|---|---|---|---|---|",
    ]
    for row in report.sources:
        padded = (*row, "—", "—")[:5]
        lines.append("| " + " | ".join(_md_cell(cell) for cell in padded) + " |")
    lines += ["", "## Metodología", ""] + [f"- {_md(item)}" for item in report.methodology]
    for section in report.sections:
        lines += ["", f"## {_md(section.title)}", ""]
        lines += [f"{_md(p)}\n" for p in section.paragraphs]
        if section.table:
            header, *rows = section.table
            lines.append("| " + " | ".join(_md_cell(c) for c in header) + " |")
            lines.append("|" + "---|" * len(header))
            lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    if report.citations:
        lines += ["", f"## {report.citations_title}", ""] + [
            f"- {_md(c)}" for c in report.citations
        ]
    if report.warnings:
        lines += ["", "## Advertencias", ""] + [f"- {_md(w)}" for w in report.warnings]
    lines += ["", "## Estado del corpus", ""] + [f"- {_md(c)}" for c in report.corpus]
    lines += ["", "---", "", report.disclaimer, ""]
    return "\n".join(lines)


_HTML_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;"
    "color:#1d1d1b;line-height:1.5}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:1.6rem}"
    "table{border-collapse:collapse;width:100%;font-size:.9rem}"
    "th,td{border:1px solid #ccc;padding:.3rem;text-align:left}"
    ".aviso{color:#8a5300}.pie{color:#555;font-size:.85rem}"
    "@media print{body{margin:0}a{color:inherit}}"
)


def to_html(report: Report) -> str:
    e = html.escape

    def table(rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]]) -> str:
        if not rows:
            return ""
        header, *body = rows
        head = "".join(f"<th>{e(c)}</th>" for c in header)
        cells = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in body)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{cells}</tbody></table>"

    parts = [
        "<!doctype html>",
        '<html lang="es"><head><meta charset="utf-8">',
        f"<title>{e(report.title)}</title><style>{_HTML_STYLE}</style></head><body>",
        f"<h1>{e(report.title)}</h1>",
        f'<p class="pie">Generado: {e(report.created_at)}. {e(report.disclaimer)}</p>',
        "<h2>Parámetros</h2><ul>",
        *(f"<li><strong>{e(k)}:</strong> {e(v)}</li>" for k, v in report.parameters),
        "</ul><h2>Informes utilizados</h2>",
        table(
            [
                ("Periodo", "Informe", "Fuente", "Adquisición", "Vigencia"),
                *(((*row, "—", "—")[:5]) for row in report.sources),
            ]
        ),  # fmt: skip
        "<h2>Metodología</h2><ul>",
        *(f"<li>{e(item)}</li>" for item in report.methodology),
        "</ul>",
    ]
    for section in report.sections:
        parts.append(f"<h2>{e(section.title)}</h2>")
        parts += [f"<p>{e(p)}</p>" for p in section.paragraphs]
        parts.append(table(section.table))
    if report.citations:
        parts += [
            f"<h2>{e(report.citations_title)}</h2><ul>",
            *(f"<li>{e(c)}</li>" for c in report.citations),
            "</ul>",
        ]
    if report.warnings:
        parts += ['<h2>Advertencias</h2><ul class="aviso">',
                  *(f"<li>{e(w)}</li>" for w in report.warnings), "</ul>"]  # fmt: skip
    parts += ["<h2>Estado del corpus</h2><ul>", *(f"<li>{e(c)}</li>" for c in report.corpus),
              f'</ul><hr><p class="pie">{e(report.disclaimer)}</p></body></html>']  # fmt: skip
    return "\n".join(parts)


def _pdf_text(text: str) -> str:
    """Texto representable con las fuentes estándar: compatibilidad Unicode (letras matemáticas,
    espacios y guiones especiales), símbolos transliterados y sin caracteres de ancho cero."""
    text = text.translate(_PDF_TRANSLITERATION)
    text = "".join(
        char if char.encode("cp1252", "ignore") else unicodedata.normalize("NFKC", char)
        for char in text
    )
    text = text.replace("\u2011", "-").replace("\u2010", "-")
    return "".join(
        char for char in text if unicodedata.category(char) not in {"Cf", "Co", "Cn", "Cs"}
    )


def to_pdf(report: Report) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Flowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("celda", parent=styles["BodyText"], fontSize=8, leading=10)
    width = A4[0] - 4 * cm  # márgenes de 2 cm

    def p(text: str, style: str = "BodyText") -> Paragraph:
        return Paragraph(html.escape(_pdf_text(text)), styles[style])

    def cell(text: str) -> Paragraph:
        # ReportLab corta las palabras largas (p. ej., URL) para no desbordar la columna.
        return Paragraph(html.escape(_pdf_text(text)), cell_style)

    def table(
        rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
        fractions: tuple[float, ...] | None = None,
    ) -> Table:
        data = [[cell(value) for value in row] for row in rows]
        columns = len(rows[0])
        shares = fractions or tuple(1 / columns for _ in range(columns))
        grid = Table(data, repeatRows=1, colWidths=[width * share for share in shares])
        grid.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                  ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                                  ("VALIGN", (0, 0), (-1, -1), "TOP")]))  # fmt: skip
        return grid

    story: list[Flowable] = [p(report.title, "Title"),
                           p(f"Generado: {report.created_at}. {report.disclaimer}")]  # fmt: skip
    story += [p("Parámetros", "Heading2")] + [p(f"{k}: {v}") for k, v in report.parameters]
    story += [p("Informes utilizados", "Heading2"),
              table([("Periodo", "Informe", "Fuente", "Adquisición", "Vigencia"),
                     *(((*row, "—", "—")[:5]) for row in report.sources)],
                    (0.11, 0.30, 0.33, 0.13, 0.13))]  # fmt: skip
    story += [p("Metodología", "Heading2")] + [p(f"• {item}") for item in report.methodology]
    for section in report.sections:
        story += [p(section.title, "Heading2")] + [p(text) for text in section.paragraphs]
        if section.table:
            story += [table(section.table, (0.14, 0.14, 0.32, 0.40)), Spacer(1, 0.3 * cm)]
    if report.citations:
        story += [p(report.citations_title, "Heading2")] + [p(f"• {c}") for c in report.citations]
    if report.warnings:
        story += [p("Advertencias", "Heading2")] + [p(f"• {w}") for w in report.warnings]
    story += [p("Estado del corpus", "Heading2")] + [p(f"• {c}") for c in report.corpus]
    story += [Spacer(1, 0.5 * cm), p(report.disclaimer)]
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, title=report.title, author="Proyecto no oficial",
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )  # fmt: skip
    document.build(story)
    return buffer.getvalue()


def render(report: Report, output_format: str) -> bytes:
    if output_format == "md":
        return to_markdown(report).encode("utf-8")
    if output_format == "html":
        return to_html(report).encode("utf-8")
    if output_format == "pdf":
        return to_pdf(report)
    raise ValueError("Formato de reporte desconocido: use md, html o pdf.")
