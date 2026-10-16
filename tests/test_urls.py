import pytest

from politica_monetaria_banrep_rag.urls import (
    UnsafeUrlError,
    download_hop_url,
    download_start_url,
    is_challenge_url,
    page_url,
)

UUID = "7a119fee-bead-43c1-9671-78e3f45ef54a"
REPO = "https://repositorio.banrep.gov.co"
LEGACY = f"{REPO}/bitstream/handle/20.500.12134/11411/IPM-julio-2026.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/rh7c",
        "https://doi.org/10.32468/INF-POL-MONT-SPA.TR3-2026",
        "https://doi.org/10.32468/inf-pol-mont-spa.tr3-2026",
        LEGACY,
        f"{REPO}/bitstream/handle/20.500.12134/10981/IPM-Octubre2024",
        "https://www.banrep.gov.co/sites/default/files/publicaciones/archivos/ipm.pdf",
    ],
)
def test_start_and_hop_accept_institutional_patterns(url):
    assert download_start_url(url) == url
    assert download_hop_url(url) == url


@pytest.mark.parametrize(
    "url",
    [f"{REPO}/bitstreams/{UUID}/download", f"{REPO}/server/api/core/bitstreams/{UUID}/content"],
)
def test_dspace_bitstreams_are_only_redirect_hops(url):
    assert download_hop_url(url) == url
    with pytest.raises(UnsafeUrlError):
        download_start_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://repositorio.banrep.gov.co/bitstreams/{UUID}/download",
        f"https://repositorio.banrep.gov.co:443/bitstreams/{UUID}/download",
        f"https://usuario:clave@repositorio.banrep.gov.co/bitstreams/{UUID}/download",
        f"https://repositorio.banrep.gov.co@evil.example/bitstreams/{UUID}/download",
        f"https://repositorio.banrep.gov.co./bitstreams/{UUID}/download",
        f"https://REPOSITORIO.banrep.gov.co/bitstreams/{UUID}/download",
        f"https://xn--repositorio-banrep.gov.co/bitstreams/{UUID}/download",
        f"https://repositorio.banrep.gov.co.evil.example/bitstreams/{UUID}/download",
        f"{REPO}/bitstreams/{UUID.upper()}/download",
        f"{REPO}/bitstreams/{UUID}/download?isAllowed=y",
        f"{REPO}/bitstreams/{UUID}/download#pagina",
        f"{REPO}/bitstreams/{UUID}/download/",
        f"{REPO}/bitstreams/{UUID}/../../server/api/eperson",
        f"{REPO}/bitstreams/%2e%2e/{UUID}/download",
        f"{REPO}/bitstream/handle/20.500.12134/11411/..%2f..%2fserver",
        f"{REPO}/bitstream/handle/20.500.12134/11411/a%5cb.pdf",
        f"{REPO}/bitstream/handle/20.500.12134/11411/a%00.pdf",
        f"{REPO}/bitstream/handle/20.500.12134/11411/a%0a.pdf",
        f"{REPO}//bitstreams/{UUID}/download",
        f"{REPO}/server/api/core/items/{UUID}",
        f"{REPO}/server/api/discover/search/objects",
        f"{REPO}/items/15ad52ae-8c9c-4355-9be8-e5031243fee3",
        f"{REPO}/bitstream/handle/99.999/1/IPM.pdf",
        "https://doi.org/10.1000/otro-editor",
        "https://doi.org/../",
        "https://doi.org/rh7c?x=1",
        "https://www.banrep.gov.co/es/informe",
        "https://validate.perfdrive.com/IPM.pdf",
        "https://127.0.0.1/IPM.pdf",
        "file:///etc/passwd",
        "https://www.banrep.gov.co/ipm .pdf",
        "https://www.banrep.gov.co\\@evil.example/ipm.pdf",
    ],
)
def test_hop_rejects_unsafe_or_unknown_urls(url):
    with pytest.raises(UnsafeUrlError):
        download_hop_url(url)
    with pytest.raises(UnsafeUrlError):
        download_start_url(url)


def test_challenge_hosts_are_detected():
    assert is_challenge_url("https://validate.perfdrive.com/?ssa=1")
    assert is_challenge_url("https://otro.perfdrive.com/")
    assert not is_challenge_url(LEGACY)
    assert not is_challenge_url("https://perfdrive.com.evil.example/")
    with pytest.raises(UnsafeUrlError, match="antibot"):
        download_hop_url("https://validate.perfdrive.com/?ssa=1")


def test_page_url_rejects_fragments_and_foreign_hosts():
    assert page_url("https://www.banrep.gov.co/es/informe/julio") == (
        "https://www.banrep.gov.co/es/informe/julio"
    )
    for url in [
        "https://www.banrep.gov.co/es/informe#x",
        "https://repositorio.banrep.gov.co/items/x",
        "https://www.banrep.gov.co/",
    ]:
        with pytest.raises(UnsafeUrlError):
            page_url(url)
