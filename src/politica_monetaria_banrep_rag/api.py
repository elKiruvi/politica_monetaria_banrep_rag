"""API HTTP local (FastAPI) sobre el servicio de consulta: ``/api/v1``.

Seguridad por defecto: escucha en 127.0.0.1, acepta solo cabeceras ``Host`` locales (protege
contra *DNS rebinding*), no habilita CORS, limita el tamaño de las solicitudes, serializa las
llamadas al modelo de lenguaje y nunca devuelve trazas internas.
"""

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from politica_monetaria_banrep_rag.retrieval import SearchFilters
from politica_monetaria_banrep_rag.service import DISCLAIMER, RagService, ServiceError

API_VERSION = "v1"
WEB_DIR = Path(__file__).parent / "web" / "static"
MAX_BODY_BYTES = 16_384
LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Cache-Control": "no-store",
}
_STATUS = {"invalid": 422, "not_found": 404, "unavailable": 503}
Period = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
ReportId = Annotated[str, Field(pattern=r"^(ipm-)?\d{4}-\d{2}$")]
Mode = Literal["auto", "lexico", "semantico", "hibrido"]


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_ids: list[Annotated[str, Field(pattern=r"^ipm-\d{4}-\d{2}$")]] = Field(
        default_factory=list, max_length=20
    )
    period_from: Period | None = None
    period_to: Period | None = None
    section: str | None = Field(default=None, min_length=1, max_length=200)
    # Tipos y calidades se validan en SearchFilters (un valor desconocido responde 422).
    kinds: list[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=8)
    evidence_qualities: list[Annotated[str, Field(max_length=30)]] = Field(
        default_factory=list, max_length=5
    )

    def to_filters(self) -> SearchFilters:
        return SearchFilters(
            report_ids=tuple(self.report_ids),
            period_from=self.period_from,
            period_to=self.period_to,
            section=self.section,
            kinds=tuple(self.kinds),
            evidence_qualities=tuple(self.evidence_qualities),
        )


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    mode: Mode = "auto"
    top_k: int = Field(default=5, ge=1, le=50)
    filters: Filters = Field(default_factory=Filters)


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    mode: Mode = "auto"
    top_k: int = Field(default=6, ge=1, le=12)
    filters: Filters = Field(default_factory=Filters)


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str | None = Field(default=None, max_length=500)
    reports: list[ReportId] = Field(default_factory=list, max_length=12)
    indicator: str | None = Field(default=None, min_length=1, max_length=200)
    horizon: str | None = Field(default=None, min_length=1, max_length=40)
    table: str | None = Field(default=None, min_length=1, max_length=200)
    include_observed: bool = False


class SummarizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: ReportId
    topic: str | None = Field(default=None, min_length=1, max_length=200)
    mode: Mode = "auto"
    top_k: int = Field(default=8, ge=1, le=12)


def _json(content: object, status_code: int = 200) -> Response:
    """JSON con ``Decimal`` como texto (sin pérdida de precisión) y sin escapar el español."""
    body = json.dumps(content, ensure_ascii=False, default=str)
    return Response(
        body, status_code=status_code, media_type="application/json", headers=SECURITY_HEADERS
    )


def _error(status_code: int, message: str, warnings: list[str] | None = None) -> Response:
    return _json(
        {"detail": message, "warnings": warnings or [], "disclaimer": DISCLAIMER}, status_code
    )


class BodyLimitMiddleware:
    """Corta en 413 cualquier cuerpo mayor que ``limit`` bytes, con ``Content-Length`` o sin él
    (``Transfer-Encoding: chunked``): cuenta lo recibido en lugar de confiar en la cabecera."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.limit):
            await _error(413, "La solicitud es demasiado grande.")(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(413, "La solicitud es demasiado grande.")
            return message

        await self.app(scope, limited, send)


def create_app(service: RagService, *, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(
        title="Informes de Política Monetaria — consulta local (no oficial)",
        version="0.1.0",
        # Sin Swagger/ReDoc: cargan recursos de un CDN y la CSP los bloquea. Queda el esquema.
        docs_url=None,
        redoc_url=None,
        openapi_url=f"/api/{API_VERSION}/openapi.json",
    )
    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)
    generation_lock = threading.Lock()

    @app.middleware("http")
    async def security(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> Response:
        return _error(_STATUS.get(exc.kind, 500), str(exc), exc.warnings)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        messages = {404: "Ruta no encontrada.", 405: "Método no permitido."}
        detail = exc.detail if exc.status_code == 413 else messages.get(exc.status_code)
        return _error(exc.status_code, str(detail or "Solicitud no válida."))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        if any(e.get("type") == "json_invalid" for e in exc.errors()):
            return _error(422, "El cuerpo de la solicitud no es JSON válido.")
        fields = sorted({".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()})
        return _error(422, "Solicitud inválida en: " + (", ".join(fields) or "cuerpo") + ".")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        return _error(500, "Error interno; revise el registro del servidor.")

    def _generate(call):  # type: ignore[no-untyped-def]
        if not generation_lock.acquire(timeout=1):
            raise ServiceError(
                "unavailable", "El modelo está atendiendo otra solicitud; reintente."
            )
        try:
            return call()
        finally:
            generation_lock.release()

    prefix = f"/api/{API_VERSION}"
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        """Interfaz web: HTML, CSS y JS locales (sin CDN); usa esta misma API."""
        return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get(f"{prefix}/health")
    def health() -> Response:
        """El proceso responde; no comprueba índice ni modelos."""
        return _json({"status": "ok"})

    @app.get(f"{prefix}/ready")
    def ready() -> Response:
        """Listo solo si hay un índice válido; informa si Ollama y el modelo están disponibles."""
        status = service.status()
        index = status["index"]
        assert isinstance(index, dict)
        body = {
            "ready": bool(index.get("available")),
            "index": index,
            "ollama": status["ollama"],
            "generation_model_installed": status["generation_model_installed"],
        }
        return _json(body, 200 if body["ready"] else 503)

    @app.get(f"{prefix}/status")
    def status() -> Response:
        return _json(service.status())

    @app.get(f"{prefix}/reports")
    def reports() -> Response:
        return _json({"reports": service.reports(), "disclaimer": DISCLAIMER})

    @app.post(f"{prefix}/search")
    def search(request: SearchRequest) -> Response:
        result = service.search(
            request.query, _filters(request.filters), top_k=request.top_k, mode=request.mode
        )
        rows = [{**hit.to_dict(), "table_notes": result.notes[hit.chunk.chunk_id]}
                for hit in result.hits]  # fmt: skip
        return _json({"index_id": result.index_id, "mode": result.mode, "results": rows,
                      "corpus": result.corpus, "warnings": result.warnings,
                      "disclaimer": DISCLAIMER})  # fmt: skip

    @app.post(f"{prefix}/query")
    def query(request: QueryRequest) -> Response:
        result = _generate(
            lambda: service.ask(
                request.question,
                _filters(request.filters),
                top_k=request.top_k,
                mode=request.mode,
            )  # fmt: skip
        )
        return _json({"index_id": result.index_id, "retrieval_mode": result.mode,
                      "generation_model": result.model,
                      "detected_reports": list(result.detected_reports),
                      **asdict(result.answer), "corpus": result.corpus,
                      "warnings": result.warnings, "disclaimer": DISCLAIMER})  # fmt: skip

    @app.post(f"{prefix}/compare")
    def compare(request: CompareRequest) -> Response:
        result = service.compare(
            question=request.question,
            reports=tuple(request.reports),
            indicator=request.indicator,
            horizon=request.horizon,
            table=request.table,
            include_observed=request.include_observed,
        )
        return _json({"index_id": result.index_id, **asdict(result.comparison),
                      "missing_reports": list(result.missing_reports),
                      "table_notes": result.table_notes, "corpus": result.corpus,
                      "warnings": result.warnings, "disclaimer": DISCLAIMER})  # fmt: skip

    @app.post(f"{prefix}/summarize")
    def summarize(request: SummarizeRequest) -> Response:
        result = _generate(
            lambda: service.summarize(
                request.report, topic=request.topic, top_k=request.top_k, mode=request.mode
            )
        )
        return _json({"index_id": result.index_id, "report": result.report.to_dict(),
                      "topic": result.topic, "generation_model": result.model,
                      **asdict(result.answer), "corpus": result.corpus,
                      "warnings": result.warnings, "disclaimer": DISCLAIMER})  # fmt: skip

    return app


def _filters(filters: Filters) -> SearchFilters:
    try:
        return filters.to_filters()
    except ValueError as exc:
        raise ServiceError("invalid", str(exc)) from exc
