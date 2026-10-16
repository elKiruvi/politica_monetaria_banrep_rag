import httpx
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: prueba que consulta fuentes reales (opt-in)")


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch, request):
    """La suite offline nunca debe salir a la red; solo `MockTransport` está permitido."""
    if request.node.get_closest_marker("live"):
        return

    def blocked(self, outgoing):
        raise RuntimeError(f"Una prueba offline intentó acceder a la red: {outgoing.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
