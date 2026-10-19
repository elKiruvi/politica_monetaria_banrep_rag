"""Detector de publicaciones: compara Crossref con una referencia versionada de informes conocidos.

Clasifica cada ejecución en ``sin_cambios``, ``cambios`` (con ``nuevo_informe``,
``documento_actualizado`` o ``correccion_metadatos`` por informe), ``fuente_no_disponible``,
``respuesta_ambigua`` o ``error_red``. Un fallo de acceso nunca se informa como «sin cambios».
La referencia solo guarda metadatos (nunca PDF) y se actualiza de forma explícita.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from politica_monetaria_banrep_rag.source import (
    ReportRecord,
    ReportSource,
    SourceBlockedError,
    SourceError,
)

REFERENCE_VERSION = 1
# Campos que identifican el documento publicado: si cambian, el PDF puede ser otro.
_DOCUMENT_FIELDS = ("download_url", "file_updated_at", "source_updates")
# Campos descriptivos: su cambio es una corrección de metadatos.
_METADATA_FIELDS = (
    "reference_period",
    "title",
    "doi",
    "published_at",
    "source_url",
    "language",
    "document_type",
)
# «discovered_at» y la fecha de depósito en Crossref cambian sin que cambie el informe.
_TRACKED = ("report_id", *_DOCUMENT_FIELDS, *_METADATA_FIELDS)


@dataclass(frozen=True)
class Change:
    kind: str  # nuevo_informe | documento_actualizado | correccion_metadatos
    report_id: str
    doi: str | None
    title: str
    fields: tuple[str, ...] = ()
    key: str = ""  # identificador estable del cambio (evita avisos duplicados)


@dataclass(frozen=True)
class Detection:
    status: str  # sin_cambios | cambios | fuente_no_disponible | respuesta_ambigua | error_red
    checked_at: str
    changes: tuple[Change, ...] = ()
    message: str = ""
    window: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def snapshot(record: ReportRecord) -> dict[str, object]:
    data = record.to_dict()
    return {name: data[name] for name in _TRACKED}


def load_reference(path: Path) -> dict[str, dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("version") != REFERENCE_VERSION
        or not isinstance(data.get("reports"), list)
    ):
        raise ValueError("Referencia de informes con formato no reconocido.")
    reports: dict[str, dict[str, object]] = {}
    for item in data["reports"]:
        if not isinstance(item, dict) or not isinstance(item.get("report_id"), str):
            raise ValueError("Informe sin identificador en la referencia.")
        if item["report_id"] in reports:
            raise ValueError(f"Informe repetido en la referencia: {item['report_id']}.")
        reports[item["report_id"]] = item
    return reports


def write_reference(path: Path, records: list[ReportRecord], *, now: datetime) -> None:
    payload = {
        "version": REFERENCE_VERSION,
        "description": "Metadatos de los informes conocidos (sin PDF); se actualiza con "
        "«banrep-rag detectar --actualizar-referencia».",
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reports": [snapshot(record) for record in records],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _key(kind: str, report_id: str, values: dict[str, object]) -> str:
    material = json.dumps({"kind": kind, "id": report_id, **values}, sort_keys=True, default=str)
    return f"{report_id}:{kind}:{hashlib.sha256(material.encode()).hexdigest()[:12]}"


def classify(reference: dict[str, dict[str, object]], current: list[ReportRecord]) -> list[Change]:
    changes = []
    for record in current:
        now = snapshot(record)
        known = reference.get(record.report_id)
        if known is None:
            changes.append(Change("nuevo_informe", record.report_id, record.doi, record.title,
                                  key=_key("nuevo_informe", record.report_id, now)))  # fmt: skip
            continue
        document = tuple(f for f in _DOCUMENT_FIELDS if known.get(f) != now[f])
        metadata = tuple(f for f in _METADATA_FIELDS if known.get(f) != now[f])
        if document:
            # Se informan también los metadatos que cambiaron junto con el documento.
            key = _key("documento_actualizado", record.report_id, now)
            changes.append(Change("documento_actualizado", record.report_id, record.doi,
                                  record.title, document + metadata, key))  # fmt: skip
        elif metadata:
            key = _key("correccion_metadatos", record.report_id, now)
            changes.append(Change("correccion_metadatos", record.report_id, record.doi,
                                  record.title, metadata, key))  # fmt: skip
    return changes


def detect(
    source: ReportSource,
    reference: dict[str, dict[str, object]],
    *,
    max_reports: int = 12,
    now: datetime | None = None,
) -> tuple[Detection, list[ReportRecord]]:
    """Consulta la fuente una vez y clasifica; nunca reintenta de forma agresiva."""
    checked_at = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        records = source.discover(max_reports)
    except SourceBlockedError as exc:
        return Detection("fuente_no_disponible", checked_at, message=str(exc)), []
    except SourceError as exc:
        cause = exc.__cause__
        if isinstance(cause, httpx.TransportError):
            return Detection("error_red", checked_at, message=str(exc)), []
        http = re.match(r"Crossref respondió HTTP (\d{3})", str(exc))
        if http and (http.group(1) == "429" or http.group(1).startswith("5")):
            return Detection("fuente_no_disponible", checked_at, message=str(exc)), []
        return Detection("respuesta_ambigua", checked_at, message=str(exc)), []
    except httpx.TransportError as exc:
        return Detection("error_red", checked_at, message=f"Fallo de red: {type(exc).__name__}"), []
    if len(records) != max_reports:
        # El contrato de la fuente es devolver la ventana completa; otra cosa no es «sin cambios».
        return Detection(
            "respuesta_ambigua",
            checked_at,
            message=f"La fuente devolvió {len(records)} de {max_reports} informes.",
        ), []
    changes = classify(reference, records)
    window = tuple(record.report_id for record in records)
    status = "cambios" if changes else "sin_cambios"
    return Detection(status, checked_at, tuple(changes), window=window), records


EXIT_CODES = {
    "sin_cambios": 0,
    "cambios": 10,
    "fuente_no_disponible": 20,
    "respuesta_ambigua": 21,
    "error_red": 22,
}
