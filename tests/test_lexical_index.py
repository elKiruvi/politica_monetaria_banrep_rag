"""Índice léxico FTS5 real sobre fragmentos sintéticos, sin PDF ni red."""

import hashlib

import pytest

from politica_monetaria_banrep_rag.corpus import chunk_ids_digest
from politica_monetaria_banrep_rag.lexical import (
    LexicalIndex,
    LexicalIndexError,
    build_lexical_index,
    build_match,
)
from politica_monetaria_banrep_rag.retrieval import (
    IndexedChunk,
    InvalidChunkError,
    ReportMeta,
    SearchFilters,
)
from politica_monetaria_banrep_rag.tables import AttributedCell


def report(period: str) -> ReportMeta:
    return ReportMeta(
        report_id=f"ipm-{period}",
        title=f"Informe de Política Monetaria {period}",
        reference_period=period,
        published_at=f"{period}-28",
        source_url=f"https://doi.org/10.32468/{period}",
        doi=f"10.32468/{period}",
        download_url=f"https://repositorio.banrep.gov.co/{period}.pdf",
        pdf_sha256=hashlib.sha256(period.encode()).hexdigest(),
        pdf_pages=60,
        acquisition_method="local_import",
    )


def chunk(
    key: str,
    period: str,
    text: str,
    *,
    kind: str = "paragraph",
    quality: str = "narrative_only",
    section: tuple[str, ...] = ("2. Pronósticos macroeconómicos",),
    page: int = 10,
    cells: tuple[AttributedCell, ...] = (),
    table_id: str | None = None,
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=hashlib.sha256(key.encode()).hexdigest()[:32],
        report_id=f"ipm-{period}",
        kind=kind,
        text=text,
        section=section,
        page_start=page,
        page_end=page,
        page_labels=(str(page + 2),),
        evidence_quality=quality,
        attributed_cells=cells,
        table_id=table_id,
    )


CELL = AttributedCell(("IPC total",), "dic-25", "4,79", "dic-25", None, True)
CHUNKS = (
    chunk("a", "2025-07", "Las expectativas de inflación se mantuvieron en el 3,5 % a dos años."),
    chunk("b", "2025-07", "La inflación básica sin alimentos ni regulados bajó.", page=12),
    chunk(
        "c",
        "2025-07",
        "Cuadro 2.1 Proyecciones Columnas: Concepto | dic-25 IPC total — dic-25: 4,79",
        kind="table",
        quality="structured_cells",
        section=("Anexo 2 Principales variables",),
        page=49,
        cells=(CELL,),
        table_id="1" * 32,
    ),
    chunk(
        "d",
        "2025-07",
        "Cuadro 2.1 Proyecciones — notas: los valores en negrilla son proyecciones.",
        kind="table_notes",
        quality="note_only",
        section=("Anexo 2 Principales variables",),
        page=49,
        table_id="1" * 32,
    ),
    chunk(
        "e",
        "2025-10",
        "Cuadro 2.1 Proyecciones — notas: datos preliminares de otro informe.",
        kind="table_notes",
        quality="note_only",
        page=47,
        table_id="2" * 32,
    ),
    chunk(
        "f",
        "2025-10",
        "Cuadro 2.1 Proyecciones Columnas: Concepto | 2025 PIB 2,6",
        kind="table",
        quality="structured_cells",
        page=47,
        cells=(AttributedCell(("PIB",), "2025", "2,6", "2025", None, False),),
        table_id="2" * 32,
    ),
    chunk(
        "g",
        "2025-10",
        "Precio del petróleo 3,5 por ciento 68 dólares por barril",
        kind="table_raw",
        quality="unattributed_values",
        page=30,
    ),
    chunk("h", "2025-10", "El precio del petróleo Brent cayó en el trimestre.", page=8),
    chunk("i", "2025-10", "El precio del petróleo Brent cayó en el trimestre.", page=9),
)
REPORTS = (report("2025-07"), report("2025-10"))


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "lexical.sqlite"
    digest = chunk_ids_digest(c.chunk_id for c in CHUNKS)
    build_lexical_index(path, REPORTS, CHUNKS, digest)
    opened = LexicalIndex(path, chunk_count=len(CHUNKS), chunk_ids_sha256=digest)
    yield opened
    opened.close()


def ids(hits):
    return [hit.chunk.chunk_id for hit in hits]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("¿Cuál fue la inflación?", '"inflacion"'),
        ("3,5 %", '"3 5 por ciento"'),
        (
            "tasa de política monetaria",
            '"tasa" OR "politica" OR "monetaria" OR "tasa de politica monetaria"',
        ),  # fmt: skip
        ('inflación" OR NEAR(x) * -y', '"inflacion" OR "or" OR "near" OR "inflacion or near x y"'),
        ("de la y el", None),
        ("", None),
    ],
)
def test_query_is_never_raw_fts_syntax(query, expected):
    assert build_match(query) == expected


def test_query_length_is_limited():
    with pytest.raises(ValueError, match="500"):
        build_match("a" * 501)


def test_exact_and_accentless_search(index):
    accented = index.search("inflación básica", SearchFilters(), 3)
    plain = index.search("inflacion basica", SearchFilters(), 3)
    assert ids(accented) == ids(plain)
    assert accented[0].chunk.text.startswith("La inflación básica")
    assert accented[0].lexical_bm25 < 0  # convención FTS5: más negativo, más relevante
    assert accented[0].report.reference_period == "2025-07"
    assert (accented[0].chunk.page_start, accented[0].chunk.page_labels) == (12, ("14",))


def test_percent_matches_written_form_and_numbers_are_phrases(index):
    hits = index.search("3,5 %", SearchFilters(), 10)
    texts = {hit.chunk.text for hit in hits}
    assert any("3,5 %" in text for text in texts)
    assert any("3,5 por ciento" in text for text in texts)
    # «3» y «5» por separado no bastan: «2,6» o «4,79» no aparecen.
    assert all("3,5" in text for text in texts)


def test_ties_are_deterministic_by_chunk_id(index):
    first = index.search("petróleo Brent trimestre", SearchFilters(kinds=("paragraph",)), 5)
    second = index.search("petróleo Brent trimestre", SearchFilters(kinds=("paragraph",)), 5)
    assert ids(first) == ids(second) == sorted(ids(first))
    assert first[0].lexical_bm25 == first[1].lexical_bm25
    assert len(set(ids(first))) == len(first) == 2  # sin duplicados


@pytest.mark.parametrize(
    "filters,expected_reports",
    [
        (SearchFilters(report_ids=("ipm-2025-10",)), {"ipm-2025-10"}),
        (SearchFilters(period_from="2025-08"), {"ipm-2025-10"}),
        (SearchFilters(period_to="2025-07"), {"ipm-2025-07"}),
        (SearchFilters(period_from="2025-07", period_to="2025-07"), {"ipm-2025-07"}),
        (SearchFilters(period_from="2026-01"), set()),
    ],
)
def test_report_and_period_filters(index, filters, expected_reports):
    hits = index.search("cuadro proyecciones notas", filters, 20)
    assert {hit.chunk.report_id for hit in hits} == expected_reports


def test_section_kind_and_quality_filters(index):
    by_section = index.search("proyecciones", SearchFilters(section="ANEXO 2 principales"), 10)
    assert by_section and all(hit.chunk.section[0].startswith("Anexo 2") for hit in by_section)
    structured = index.search(
        "proyecciones", SearchFilters(evidence_qualities=("structured_cells",)), 10
    )
    assert {hit.chunk.kind for hit in structured} == {"table"}
    assert all(hit.chunk.attributed_cells for hit in structured)
    notes = index.search("proyecciones", SearchFilters(kinds=("table_notes",)), 10)
    assert {hit.chunk.evidence_quality for hit in notes} == {"note_only"}
    assert all(not hit.chunk.attributed_cells for hit in notes)


def test_unattributed_table_keeps_its_quality(index):
    hits = index.search("petróleo dólares por barril", SearchFilters(kinds=("table_raw",)), 5)
    assert len(hits) == 1
    assert hits[0].chunk.evidence_quality == "unattributed_values"
    assert hits[0].chunk.attributed_cells == ()
    assert hits[0].chunk.table_id is None
    assert index.table_notes(hits[0].chunk) == []


def test_attributed_cells_round_trip_exactly(index):
    hit = index.search("IPC total", SearchFilters(kinds=("table",)), 1)[0]
    assert hit.chunk.attributed_cells == (CELL,)
    assert hit.chunk.attributed_cells[0].projection_or_assumption is True
    assert hit.chunk.attributed_cells[0].unit is None  # no se completa


def test_table_notes_follow_table_id_not_caption(index):
    table = index.search("IPC total", SearchFilters(kinds=("table",)), 1)[0].chunk
    notes = index.table_notes(table)
    assert [note.text for note in notes] == [
        "Cuadro 2.1 Proyecciones — notas: los valores en negrilla son proyecciones."
    ]
    other = index.search("PIB", SearchFilters(report_ids=("ipm-2025-10",), kinds=("table",)), 1)
    assert [n.text for n in index.table_notes(other[0].chunk)] == [
        "Cuadro 2.1 Proyecciones — notas: datos preliminares de otro informe."
    ]


def test_no_results_and_stopword_only_queries(index):
    assert index.search("zzzz inexistente", SearchFilters(), 5) == []
    assert index.search("de la", SearchFilters(), 5) == []


def test_limit_is_validated(index):
    with pytest.raises(ValueError):
        index.search("inflación", SearchFilters(), 0)


def test_index_rejects_mismatched_manifest_values(tmp_path):
    path = tmp_path / "lexical.sqlite"
    digest = chunk_ids_digest(c.chunk_id for c in CHUNKS)
    build_lexical_index(path, REPORTS, CHUNKS, digest)
    with pytest.raises(LexicalIndexError):
        LexicalIndex(path, chunk_count=len(CHUNKS) - 1, chunk_ids_sha256=digest)
    with pytest.raises(LexicalIndexError):
        LexicalIndex(path, chunk_count=len(CHUNKS), chunk_ids_sha256="0" * 64)
    with pytest.raises(LexicalIndexError, match="ya existe"):
        build_lexical_index(path, REPORTS, CHUNKS, digest)


def test_corrupt_database_is_rejected(tmp_path):
    path = tmp_path / "lexical.sqlite"
    path.write_bytes(b"no es sqlite" * 100)
    with pytest.raises(LexicalIndexError):
        LexicalIndex(path, chunk_count=1, chunk_ids_sha256="0" * 64)
    with pytest.raises(LexicalIndexError):
        LexicalIndex(tmp_path / "falta.sqlite", chunk_count=1, chunk_ids_sha256="0" * 64)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"report_ids": ("2025-07",)},
        {"period_from": "2025-13"},
        {"period_from": "2025-10", "period_to": "2025-07"},
        {"kinds": ("imagen",)},
        {"evidence_qualities": ("verificado",)},
        {"section": "   "},
    ],
)
def test_invalid_filters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        SearchFilters(**kwargs)


def test_filters_python_semantics_match_sql(index):
    filters = SearchFilters(period_from="2025-08", section="pronosticos", kinds=("paragraph",))
    sql = ids(index.search("petróleo precio", filters, 20))
    reports = {r.report_id: r for r in REPORTS}
    python = sorted(
        c.chunk_id
        for c in CHUNKS
        if filters.matches(c, reports[c.report_id]) and "petróleo" in c.text
    )
    assert sorted(sql) == python


@pytest.mark.parametrize(
    "change,error",
    [
        ({"page_start": 0}, "páginas"),
        ({"page_end": 61}, "páginas"),
        ({"kind": "imagen"}, "tipo"),
        ({"evidence_quality": "verificado"}, "tipo o calidad"),
        ({"attributed_cells": [CELL.__dict__], "evidence_quality": "narrative_only"}, "celdas"),
        ({"table_id": None, "kind": "table_notes"}, "vínculo"),
        ({"text": "  "}, "vacío"),
        ({"chunk_id": "../x"}, "identificador"),
    ],
)
def test_chunk_contract_is_enforced(change, error):
    data = {**CHUNKS[0].to_dict(), **change}
    with pytest.raises(InvalidChunkError, match=error):
        IndexedChunk.from_dict(data, pages=60)
