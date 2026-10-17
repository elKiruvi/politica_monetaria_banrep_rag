"""Ejecución aislada para analizar PDF no confiables.

Los analizadores (pypdf, pdfminer) no tienen límite de tiempo y pueden consumir mucha memoria
con un PDF hostil. Aquí se ejecutan en un proceso hijo con tiempo máximo y, en Linux/macOS,
límite de memoria; si se exceden, el hijo se termina y se informa un error en español.
"""

import multiprocessing
import queue
import sys
import time
from collections.abc import Callable
from typing import Any


class IsolationError(Exception):
    """El análisis aislado falló, superó el tiempo o la memoria permitidos."""


def _child(channel: Any, function: Callable[..., Any], args: tuple[Any, ...], memory: int) -> None:
    if memory and sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    try:
        channel.put(("ok", function(*args)))
    except MemoryError:
        channel.put(("error", "MemoryError", "el análisis superó la memoria permitida"))
    except RecursionError:
        channel.put(("error", "RecursionError", "el PDF tiene estructuras anidadas en exceso"))
    except Exception as exc:  # noqa: BLE001 - se informa al proceso principal
        channel.put(("error", type(exc).__name__, str(exc)))


def run_isolated(
    function: Callable[..., Any],
    *args: Any,
    timeout: float = 120,
    memory_mb: int = 2048,
) -> Any:
    """Ejecuta ``function(*args)`` en otro proceso; la función debe ser importable (picklable).

    Las excepciones del hijo se devuelven como ``IsolationError`` con el tipo original en
    ``error_type`` para que quien llama pueda traducirlas.
    """
    channel: Any = None
    try:
        context = multiprocessing.get_context("spawn")
        channel = context.Queue()
        process = context.Process(
            target=_child, args=(channel, function, args, memory_mb * 1024 * 1024), daemon=True
        )
        process.start()
    except (OSError, RuntimeError, TypeError, ValueError, MemoryError) as exc:
        if channel is not None:
            channel.close()
        raise IsolationError("No se pudo iniciar el proceso aislado para analizar el PDF.") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                status, *payload = channel.get(timeout=0.2)
                break
            except queue.Empty:
                if not process.is_alive():
                    try:
                        status, *payload = channel.get(timeout=0.5)
                        break
                    except queue.Empty:
                        error = IsolationError(
                            "El análisis del PDF terminó de forma inesperada "
                            "(posible falta de memoria)."
                        )
                        error.error_type = "Crash"  # type: ignore[attr-defined]
                        raise error from None
                if time.monotonic() > deadline:
                    error = IsolationError(
                        "El análisis del PDF superó el tiempo máximo y se detuvo."
                    )
                    error.error_type = "Timeout"  # type: ignore[attr-defined]
                    raise error from None
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        channel.close()
    if status == "ok":
        return payload[0]
    error_type, message = payload
    error = IsolationError(message)
    error.error_type = error_type  # type: ignore[attr-defined]
    raise error
