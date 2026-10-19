"""Índice léxico con SQLite FTS5 y BM25; también guarda los metadatos citables del índice.

Decisiones (ADR 0007):

- Tokenizador ``unicode61 remove_diacritics 2``: «inflacion» encuentra «inflación». El texto
  guardado y devuelto es el original; solo la columna indexada se normaliza.
- ``%`` se indexa como «por ciento», de modo que «3,5 %» y «3,5 por ciento» coinciden. Las cifras
  se buscan como frase: «3,5» exige los tokens «3» y «5» contiguos.
- La consulta del usuario nunca llega como sintaxis FTS5: se convierte en términos entre comillas
  unidos por ``OR`` (más una frase exacta en consultas cortas), sin palabras vacías.
- ``bm25()`` devuelve valores más negativos para mejores resultados. Se ordena por (bm25,
  chunk_id) para un orden determinista y el valor no se mezcla con otras puntuaciones.
"""

import json
import re
import sqlite3
from pathlib import Path

from politica_monetaria_banrep_rag.corpus import chunk_ids_digest
from politica_monetaria_banrep_rag.retrieval import (
    IndexedChunk,
    ReportMeta,
    RetrievalHit,
    SearchFilters,
    fold,
)

LEXICAL_FORMAT = "fts5-1"
TOKENIZER = "unicode61 remove_diacritics 2"
MAX_QUERY_CHARS = 500
_MAX_TERMS = 32
_PHRASE_MAX_TOKENS = 6
# Peso de la columna de sección frente al texto en bm25().
_SECTION_WEIGHT = 0.5
_STOPWORDS = frozenset(
    """a al algo ante como con cual cuales cuando de del desde donde e el ella ellas ellos en
    entre era es esa ese eso esta este esto fue fueron ha han hay la las le les lo los mas me
    mi muy no o para pero por porque que qué se segun si sin sobre su sus tambien te tiene u un
    una uno unos unas y ya dice dijo informo""".split()
)
_TERM = re.compile(r"\d+(?:[.,]\d+)*(?:\s+por\s+ciento)?|\w+")

_SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE reports (
    report_id TEXT PRIMARY KEY,
    reference_period TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE chunks (
    rowid INTEGER PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    kind TEXT NOT NULL,
    evidence_quality TEXT NOT NULL,
    section_key TEXT NOT NULL,
    table_id TEXT,
    data TEXT NOT NULL
);
CREATE INDEX chunks_table ON chunks(report_id, table_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(body, section, tokenize="{TOKENIZER}");
"""


class LexicalIndexError(Exception):
    """El índice léxico falta, está dañado o no corresponde a su manifiesto."""


def normalize_for_index(text: str) -> str:
    return text.replace("%", " por ciento ")


def build_match(query: str) -> str | None:
    """Convierte texto libre en una expresión FTS5 segura; ``None`` si no queda ningún término."""
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"La consulta supera {MAX_QUERY_CHARS} caracteres.")
    tokens: list[str] = []
    terms: list[str] = []
    for match in _TERM.finditer(normalize_for_index(query)):
        words = re.findall(r"\w+", match.group())
        tokens.extend(words)
        if match.group()[0].isdigit():
            terms.append(" ".join(words))
        elif fold(match.group()) not in _STOPWORDS and len(match.group()) > 1:
            terms.append(match.group())
    unique = list(dict.fromkeys(fold(term) for term in terms))[:_MAX_TERMS]
    if not unique:
        return None
    # Las comillas dentro de una frase FTS5 se escapan duplicándolas; \w no las contiene.
    parts = [f'"{term}"' for term in unique]
    if len(unique) > 1 and len(tokens) <= _PHRASE_MAX_TOKENS:
        parts.append('"' + " ".join(fold(token) for token in tokens) + '"')
    return " OR ".join(parts)


def build_lexical_index(
    path: Path, reports: tuple[ReportMeta, ...], chunks: tuple[IndexedChunk, ...], digest: str
) -> None:
    """Crea la base en ``path`` (que no debe existir) y comprueba su integridad antes de cerrar."""
    if path.exists():
        raise LexicalIndexError("La ruta del índice léxico ya existe.")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SCHEMA)
        with connection:
            connection.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [
                    ("format", LEXICAL_FORMAT),
                    ("tokenizer", TOKENIZER),
                    ("chunk_count", str(len(chunks))),
                    ("chunk_ids_sha256", digest),
                ],
            )
            connection.executemany(
                "INSERT INTO reports VALUES (?, ?, ?)",
                [
                    (r.report_id, r.reference_period, json.dumps(r.to_dict(), ensure_ascii=False))
                    for r in reports
                ],
            )
            for rowid, chunk in enumerate(chunks, start=1):
                section = " > ".join(chunk.section)
                connection.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rowid,
                        chunk.chunk_id,
                        chunk.report_id,
                        chunk.kind,
                        chunk.evidence_quality,
                        fold(section),
                        chunk.table_id,
                        json.dumps(chunk.to_dict(), ensure_ascii=False),
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, body, section) VALUES (?, ?, ?)",
                    (rowid, normalize_for_index(chunk.text), section),
                )
        with connection:
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
            connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise LexicalIndexError("SQLite informó un índice léxico inconsistente.")
    finally:
        connection.close()


class LexicalIndex:
    """Lectura de un índice léxico inmutable; no lo modifica ni crea archivos auxiliares."""

    def __init__(self, path: Path, *, chunk_count: int, chunk_ids_sha256: str) -> None:
        if not path.is_file() or path.is_symlink():
            raise LexicalIndexError("Falta el índice léxico.")
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        try:
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False)
            meta = dict(self._db.execute("SELECT key, value FROM meta").fetchall())
            ids = [row[0] for row in self._db.execute("SELECT chunk_id FROM chunks")]
            fts_rows = self._db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        except sqlite3.Error as exc:
            raise LexicalIndexError(f"El índice léxico no es legible ({exc}).") from exc
        if meta != {
            "format": LEXICAL_FORMAT,
            "tokenizer": TOKENIZER,
            "chunk_count": str(chunk_count),
            "chunk_ids_sha256": chunk_ids_sha256,
        }:
            self._db.close()
            raise LexicalIndexError("El índice léxico no corresponde a su manifiesto.")
        if (
            len(ids) != chunk_count
            or fts_rows != chunk_count
            or (chunk_ids_digest(ids) != chunk_ids_sha256)
        ):
            self._db.close()
            raise LexicalIndexError("Los fragmentos del índice léxico no coinciden.")
        self._reports = {
            report_id: ReportMeta(**json.loads(data))
            for report_id, data in self._db.execute("SELECT report_id, data FROM reports")
        }

    def close(self) -> None:
        self._db.close()

    @property
    def reports(self) -> dict[str, ReportMeta]:
        return dict(self._reports)

    def _filter_sql(self, filters: SearchFilters) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if filters.report_ids:
            clauses.append(f"c.report_id IN ({','.join('?' * len(filters.report_ids))})")
            params.extend(filters.report_ids)
        if filters.period_from:
            clauses.append("r.reference_period >= ?")
            params.append(filters.period_from)
        if filters.period_to:
            clauses.append("r.reference_period <= ?")
            params.append(filters.period_to)
        if filters.section:
            clauses.append("instr(c.section_key, ?) > 0")
            params.append(fold(filters.section.strip()))
        if filters.kinds:
            clauses.append(f"c.kind IN ({','.join('?' * len(filters.kinds))})")
            params.extend(filters.kinds)
        if filters.evidence_qualities:
            qualities = filters.evidence_qualities
            clauses.append(f"c.evidence_quality IN ({','.join('?' * len(qualities))})")
            params.extend(qualities)
        return "".join(f" AND {clause}" for clause in clauses), params

    def search(self, query: str, filters: SearchFilters, limit: int) -> list[RetrievalHit]:
        if not 1 <= limit <= 200:
            raise ValueError("La cantidad de resultados debe estar entre 1 y 200.")
        match = build_match(query)
        if match is None:
            return []
        where, params = self._filter_sql(filters)
        rows = self._db.execute(
            "SELECT c.data, bm25(chunks_fts, 1.0, ?) AS score "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "JOIN reports r ON r.report_id = c.report_id "
            f"WHERE chunks_fts MATCH ?{where} ORDER BY score, c.chunk_id LIMIT ?",
            [_SECTION_WEIGHT, match, *params, limit],
        ).fetchall()
        hits = []
        for rank, (data, score) in enumerate(rows, start=1):
            chunk = self._chunk(data)
            hits.append(RetrievalHit(rank, chunk, self._reports[chunk.report_id], score))
        return hits

    def _chunk(self, data: str) -> IndexedChunk:
        report_id = json.loads(data)["report_id"]
        return IndexedChunk.from_dict(json.loads(data), pages=self._reports[report_id].pdf_pages)

    def all_chunks(self) -> list[IndexedChunk]:
        rows = self._db.execute("SELECT data FROM chunks ORDER BY rowid").fetchall()
        return [self._chunk(data) for (data,) in rows]

    def table_notes(self, chunk: IndexedChunk) -> list[IndexedChunk]:
        """Notas completas del mismo cuadro (por ``table_id`` e informe, nunca por título)."""
        if chunk.table_id is None:
            return []
        rows = self._db.execute(
            "SELECT data FROM chunks WHERE report_id = ? AND table_id = ? "
            "AND kind = 'table_notes' ORDER BY rowid",
            (chunk.report_id, chunk.table_id),
        ).fetchall()
        return [self._chunk(data) for (data,) in rows]
