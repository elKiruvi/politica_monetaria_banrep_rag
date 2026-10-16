"""Política de URL oficiales: qué se puede solicitar al inicio y qué saltos se pueden seguir.

Las reglas son deliberadamente estrechas. Una URL de inicio es la que publica la fuente
(DOI, PDF del sitio institucional o ruta heredada del repositorio). Las rutas de DSpace bajo
``/server`` solo se aceptan como saltos emitidos por el propio repositorio: ``robots.txt``
prohíbe usarlas para descubrir contenido y este proyecto nunca las solicita por iniciativa propia.
"""

import re
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit

SITE_HOSTS = frozenset({"www.banrep.gov.co", "banrep.gov.co"})
REPOSITORY_HOST = "repositorio.banrep.gov.co"
DOI_HOST = "doi.org"
# Hosts de verificación antibot conocidos: se informan como bloqueo, nunca se siguen.
CHALLENGE_HOSTS = frozenset({"validate.perfdrive.com"})

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DOI_PATH = re.compile(r"/(?:10\.32468/[A-Za-z0-9._()-]{1,120}|[a-z0-9]{1,16})")
_LEGACY_BITSTREAM = re.compile(
    r"/bitstream/handle/20\.500\.12134/\d{1,9}/[A-Za-z0-9._~()%-]{1,200}"
)
_BITSTREAM_DOWNLOAD = re.compile(rf"/bitstreams/{_UUID}/download")
_BITSTREAM_CONTENT = re.compile(rf"/server/api/core/bitstreams/{_UUID}/content")


class UnsafeUrlError(ValueError):
    """La URL no cumple la política de destinos oficiales."""


def _check_text(value: str) -> None:
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zs"} for char in value) or "\\" in value:
        raise UnsafeUrlError("La URL contiene espacios, caracteres de control o barras invertidas.")


def _parts(url: str, allowed_hosts: frozenset[str]):
    _check_text(url)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in allowed_hosts:
        raise UnsafeUrlError("La URL está fuera de los dominios autorizados.")
    if "@" in parts.netloc or parts.username or parts.password:
        raise UnsafeUrlError("La URL incluye credenciales.")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("La URL tiene un puerto no válido.") from exc
    if port is not None or parts.netloc != parts.hostname:
        raise UnsafeUrlError("La URL no puede indicar puerto ni variantes del dominio.")
    decoded = unquote(parts.path)
    _check_text(decoded)
    if "%" in decoded:
        # Un escape anidado (p. ej. %252e) podría decodificarse otra vez más adelante.
        raise UnsafeUrlError("La URL contiene escapes anidados.")
    segments = parts.path.split("/")[1:]
    if (
        not parts.path.startswith("/")
        or any(part in {"", ".", ".."} for part in decoded.split("/")[1:])
        or any(part in {"", ".", ".."} for part in segments)
        or decoded.count("/") != parts.path.count("/")
        or "%00" in parts.path
    ):
        raise UnsafeUrlError(
            "La URL tiene una ruta vacía, ambigua o codificada de forma peligrosa."
        )
    return parts


def page_url(url: str) -> str:
    """URL de una ficha o página del sitio institucional (nunca de descarga)."""
    parts = _parts(url, SITE_HOSTS)
    if parts.fragment:
        raise UnsafeUrlError("La URL de la página no puede tener fragmento.")
    return url


def _download_path_ok(host: str | None, path: str, *, hop: bool) -> bool:
    if host == DOI_HOST:
        return _DOI_PATH.fullmatch(path) is not None
    if host in SITE_HOSTS:
        return path.lower().endswith(".pdf")
    if host == REPOSITORY_HOST:
        if _LEGACY_BITSTREAM.fullmatch(path):
            return True
        return hop and bool(
            _BITSTREAM_DOWNLOAD.fullmatch(path) or _BITSTREAM_CONTENT.fullmatch(path)
        )
    return False


def download_start_url(url: str) -> str:
    """Primera URL de una descarga: DOI, PDF institucional o ruta heredada del repositorio."""
    parts = _parts(url, SITE_HOSTS | {DOI_HOST, REPOSITORY_HOST})
    if parts.query or parts.fragment:
        raise UnsafeUrlError("La URL de descarga no puede tener parámetros ni fragmento.")
    if not _download_path_ok(parts.hostname, parts.path, hop=False):
        raise UnsafeUrlError("La URL de descarga no coincide con un patrón institucional conocido.")
    return url


def download_hop_url(url: str, *, issued_by: str) -> str:
    """Destino de una redirección emitida por ``issued_by``.

    Los bitstreams de DSpace solo se aceptan si la redirección la emite el propio repositorio;
    un DOI u otro host no puede enviar la descarga directamente a esas rutas.
    """
    if is_challenge_url(url):
        raise UnsafeUrlError("La redirección conduce a una verificación antibot.")
    parts = _parts(url, SITE_HOSTS | {DOI_HOST, REPOSITORY_HOST})
    if parts.query or parts.fragment:
        raise UnsafeUrlError("La redirección de descarga no puede tener parámetros ni fragmento.")
    from_repository = urlsplit(issued_by).hostname == REPOSITORY_HOST
    if not _download_path_ok(parts.hostname, parts.path, hop=from_repository):
        raise UnsafeUrlError("La redirección no coincide con un patrón institucional conocido.")
    return url


def redirect_target(current: str, location: str) -> str:
    """Valida el ``Location`` crudo antes de unirlo, porque ``urljoin`` normaliza «.» y «..»."""
    _check_text(location)
    raw_path = urlsplit(location).path
    if any(part in {".", ".."} for part in raw_path.split("/") + unquote(raw_path).split("/")):
        raise UnsafeUrlError("La redirección tiene segmentos de ruta ambiguos.")
    return download_hop_url(urljoin(current, location), issued_by=current)


def is_challenge_url(url: str) -> bool:
    """Indica si la URL pertenece a un servicio de verificación antibot conocido."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in CHALLENGE_HOSTS or (host or "").endswith(".perfdrive.com")
