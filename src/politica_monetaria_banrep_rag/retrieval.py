"""Tipos compartidos de la recuperación: fragmentos indexados, filtros y resultados citables.

Un resultado conserva los campos de evidencia de F4 (calidad, celdas atribuibles, ``table_id``)
tal como los produjo el procesamiento; nada se completa por inferencia.
"""

import re
import unicodedata
from dataclasses import asdict, dataclass

from politica_monetaria_banrep_rag.tables import AttributedCell

CHUNK_KINDS = frozenset(
    {
        "paragraph",
        "table",
        "table_notes",
        "table_raw",
        "chart_caption",
        "caption",
        "source_note",
        "footnote",
    }
)
EVIDENCE_QUALITIES = frozenset(
    {"structured_cells", "unattributed_values", "caption_only", "narrative_only", "note_only"}
)
# Calidades posibles por tipo de fragmento, según processing.Unit y chunking.chunk_document.
_KIND_QUALITIES = {
    "paragraph": {"narrative_only"},
    "table": {"structured_cells", "unattributed_values"},
    "table_notes": {"note_only"},
    "table_raw": {"unattributed_values"},
    "chart_caption": {"caption_only"},
    "caption": {"caption_only"},
    "source_note": {"note_only"},
    "footnote": {"note_only"},
}
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_REPORT_ID = re.compile(r"ipm-\d{4}-\d{2}")
_CHUNK_ID = re.compile(r"[0-9a-f]{32}")


def fold(text: str) -> str:
    """Minúsculas sin tildes, igual que ``remove_diacritics 2`` del tokenizador de FTS5."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


class InvalidChunkError(ValueError):
    """Un fragmento guardado no cumple el contrato de evidencia."""


def _texts(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise InvalidChunkError(f"campo {field} inválido")
    return tuple(value)


def _optional_text(value: object, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise InvalidChunkError(f"campo {field} inválido")
    return value


def _cell(value: object) -> AttributedCell:
    if not isinstance(value, dict) or set(value) != {
        "row",
        "column",
        "value",
        "horizon",
        "unit",
        "projection_or_assumption",
    }:
        raise InvalidChunkError("celda atribuida inválida")
    if (
        not isinstance(value["column"], str)
        or not isinstance(value["value"], str)
        or type(value["projection_or_assumption"]) is not bool
    ):
        raise InvalidChunkError("celda atribuida inválida")
    return AttributedCell(
        row=_texts(value["row"], "row"),
        column=value["column"],
        value=value["value"],
        horizon=_optional_text(value["horizon"], "horizon"),
        unit=_optional_text(value["unit"], "unit"),
        projection_or_assumption=value["projection_or_assumption"],
    )


@dataclass(frozen=True)
class ReportMeta:
    """Metadatos citables del informe, copiados del snapshot con el que se construyó el índice."""

    report_id: str
    title: str
    reference_period: str
    published_at: str | None
    source_url: str
    doi: str | None
    download_url: str
    pdf_sha256: str
    pdf_pages: int
    acquisition_method: str

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "ReportMeta":
        report, pdf = record["report"], record["pdf"]
        acquisition = record.get("acquisition") or {"method": "download"}
        assert isinstance(report, dict) and isinstance(pdf, dict) and isinstance(acquisition, dict)
        return cls(
            report_id=str(report["report_id"]),
            title=str(report["title"]),
            reference_period=str(report["reference_period"]),
            published_at=_optional_text(report.get("published_at"), "published_at"),
            source_url=str(report["source_url"]),
            doi=_optional_text(report.get("doi"), "doi"),
            download_url=str(report["download_url"]),
            pdf_sha256=str(pdf["sha256"]),
            pdf_pages=int(str(pdf["pages"])),
            acquisition_method=str(acquisition["method"]),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    report_id: str
    kind: str
    text: str
    section: tuple[str, ...]
    page_start: int
    page_end: int
    page_labels: tuple[str, ...]
    evidence_quality: str
    attributed_cells: tuple[AttributedCell, ...]
    table_id: str | None

    @classmethod
    def from_dict(cls, data: object, *, pages: int) -> "IndexedChunk":
        """Valida un fragmento procesado; ``pages`` es el total de páginas del PDF."""
        if not isinstance(data, dict):
            raise InvalidChunkError("fragmento inválido")
        start, end = data.get("page_start"), data.get("page_end")
        chunk = cls(
            chunk_id=str(data.get("chunk_id")),
            report_id=str(data.get("report_id")),
            kind=str(data.get("kind")),
            text=data["text"] if isinstance(data.get("text"), str) else "",
            section=_texts(data.get("section"), "section"),
            page_start=start if type(start) is int else 0,
            page_end=end if type(end) is int else 0,
            page_labels=_texts(data.get("page_labels"), "page_labels"),
            evidence_quality=str(data.get("evidence_quality")),
            attributed_cells=tuple(_cell(cell) for cell in data.get("attributed_cells") or ()),
            table_id=_optional_text(data.get("table_id"), "table_id"),
        )
        if not _CHUNK_ID.fullmatch(chunk.chunk_id) or not _REPORT_ID.fullmatch(chunk.report_id):
            raise InvalidChunkError("identificador de fragmento inválido")
        if chunk.kind not in CHUNK_KINDS or chunk.evidence_quality not in EVIDENCE_QUALITIES:
            raise InvalidChunkError(f"tipo o calidad desconocidos en {chunk.chunk_id}")
        if chunk.evidence_quality not in _KIND_QUALITIES[chunk.kind]:
            raise InvalidChunkError(f"calidad incompatible con el tipo en {chunk.chunk_id}")
        if not chunk.text.strip():
            raise InvalidChunkError(f"fragmento vacío {chunk.chunk_id}")
        if not 1 <= chunk.page_start <= chunk.page_end <= pages:
            raise InvalidChunkError(f"páginas fuera del PDF en {chunk.chunk_id}")
        if chunk.attributed_cells and chunk.evidence_quality != "structured_cells":
            raise InvalidChunkError(f"celdas atribuidas sin estructura en {chunk.chunk_id}")
        if (chunk.kind in {"table", "table_notes"}) != (chunk.table_id is not None):
            raise InvalidChunkError(f"vínculo de cuadro inválido en {chunk.chunk_id}")
        return chunk

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchFilters:
    report_ids: tuple[str, ...] = ()
    period_from: str | None = None  # AAAA-MM del periodo de referencia, inclusivo
    period_to: str | None = None
    section: str | None = None  # subcadena, sin distinguir mayúsculas ni tildes
    kinds: tuple[str, ...] = ()
    evidence_qualities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not _REPORT_ID.fullmatch(report_id) for report_id in self.report_ids):
            raise ValueError("Los informes se indican como ipm-AAAA-MM.")
        for period in (self.period_from, self.period_to):
            if period is not None and not _PERIOD.fullmatch(period):
                raise ValueError("Los periodos se indican como AAAA-MM.")
        if self.period_from and self.period_to and self.period_from > self.period_to:
            raise ValueError("El periodo inicial es posterior al final.")
        if self.section is not None and not 1 <= len(self.section.strip()) <= 200:
            raise ValueError("El filtro de sección debe tener entre 1 y 200 caracteres.")
        if not set(self.kinds) <= CHUNK_KINDS:
            raise ValueError(f"Tipos válidos: {', '.join(sorted(CHUNK_KINDS))}.")
        if not set(self.evidence_qualities) <= EVIDENCE_QUALITIES:
            raise ValueError(f"Calidades válidas: {', '.join(sorted(EVIDENCE_QUALITIES))}.")

    def matches(self, chunk: IndexedChunk, report: ReportMeta) -> bool:
        """Misma semántica que los filtros SQL; la usan las vías que no consultan SQLite."""
        return (
            (not self.report_ids or chunk.report_id in self.report_ids)
            and (self.period_from is None or report.reference_period >= self.period_from)
            and (self.period_to is None or report.reference_period <= self.period_to)
            and (
                self.section is None
                or fold(self.section.strip()) in fold(" > ".join(chunk.section))
            )
            and (not self.kinds or chunk.kind in self.kinds)
            and (not self.evidence_qualities or chunk.evidence_quality in self.evidence_qualities)
        )


@dataclass(frozen=True)
class RetrievalHit:
    """Un fragmento recuperado con su informe y las señales de cada vía.

    ``lexical_bm25`` sigue la convención de FTS5 (más negativo = más relevante);
    ``semantic_similarity`` es el coseno con la consulta. Ninguna se compara con la otra: la
    fusión usa solo las posiciones (``lexical_rank``, ``semantic_rank``) en ``fused_score``.
    """

    rank: int
    chunk: IndexedChunk
    report: ReportMeta
    lexical_bm25: float | None = None
    semantic_similarity: float | None = None
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    fused_score: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "lexical_bm25": self.lexical_bm25,
            "semantic_similarity": self.semantic_similarity,
            "lexical_rank": self.lexical_rank,
            "semantic_rank": self.semantic_rank,
            "fused_score": self.fused_score,
            **self.chunk.to_dict(),
            "report": self.report.to_dict(),
        }
