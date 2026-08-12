"""TCP transport and factories for Harp devices."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from harp.device import Device, DeviceName, SerialNumber, TransportError, WhoAmI

D = TypeVar("D", bound=Device)
SetupCallback = Callable[[socket.socket], None]

DEFAULT_PORT: int = 9999
DEFAULT_RECV_SIZE: int = 4096
DEFAULT_TIMEOUT: float = 3.0

_logger = logging.getLogger(__name__)


class TcpTransport:
    """A TCP :class:`~harp.device.ITransport` (structural conformance).

    The transport owns an accepted socket. ESP32 Harp firmware initiates the
    connection to the Python host; it does not expose a TCP server to which
    Python can connect.
    """

    def __init__(
        self,
        sock: socket.socket,
        timeout: float = DEFAULT_TIMEOUT,
        recv_size: int = DEFAULT_RECV_SIZE,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if recv_size <= 0:
            raise ValueError("recv_size must be greater than zero")

        self._sock: socket.socket | None = sock
        self._timeout = timeout
        self._recv_size = recv_size
        self._peer: str | None = None
        self._is_open = False

    def open(self) -> None:
        """Prepare the accepted socket for Harp traffic."""
        if self._sock is None:
            raise TransportError("Accepted TCP connection is closed")
        try:
            host, port = self._sock.getpeername()[:2]
            self._peer = f"{host}:{port}"
            self._sock.settimeout(self._timeout)
            _set_tcp_nodelay(self._sock)
        except OSError as exc:
            raise TransportError(f"Failed to open accepted TCP connection: {exc}") from exc
        self._is_open = True

    def write(self, data: bytes) -> None:
        assert self._sock is not None and self._is_open
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise TransportError(str(exc)) from exc

    def read(self) -> bytes:
        assert self._sock is not None and self._is_open
        try:
            data = self._sock.recv(self._recv_size)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise TransportError(str(exc)) from exc
        if not data:
            raise TransportError(f"TCP connection to {self.peer} was closed by the peer")
        return data

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._is_open = False

    @property
    def peer(self) -> str:
        """Remote address of the accepted ESP32 connection."""
        return self._peer or "unknown"

    def __repr__(self) -> str:
        state = "open" if self._is_open else "closed"
        return f"<TcpTransport {self.peer} [{state}]>"


@dataclass(frozen=True, slots=True)
class TcpDeviceIdentity:
    """Identity registers cached while a device is discovered."""

    who_am_i: int
    name: str
    serial_number: int
    peer: str


class TcpDeviceList(list[D]):
    """Discovered TCP devices with identity lookups and managed cleanup."""

    def __init__(self, devices: Iterable[tuple[D, TcpDeviceIdentity]] = ()) -> None:
        entries = list(devices)
        super().__init__(device for device, _ in entries)
        self._identities = {id(device): identity for device, identity in entries}

    def __enter__(self) -> "TcpDeviceList[D]":
        return self

    def __exit__(self, *args: object) -> None:
        for device in self:
            try:
                device.close()
            except Exception:
                _logger.exception("Failed to close discovered TCP device")

    def identity(self, device: D) -> TcpDeviceIdentity:
        """Return the identity cached for ``device`` during discovery."""
        try:
            return self._identities[id(device)]
        except KeyError as exc:
            raise ValueError("device is not part of this discovery result") from exc

    def by_name(self, name: str) -> D | None:
        """Return the first device whose name starts with ``name`` (case-insensitive)."""
        prefix = name.casefold()
        return next(
            (
                device
                for device in self
                if self._identities[id(device)].name.casefold().startswith(prefix)
            ),
            None,
        )

    def by_id(self, device_id: int) -> D | None:
        """Return the first device with the requested ``WhoAmI`` value."""
        return next(
            (
                device
                for device in self
                if self._identities[id(device)].who_am_i == device_id
            ),
            None,
        )

    def by_serial(self, serial_number: int) -> D | None:
        """Return the first device with the requested serial number."""
        return next(
            (
                device
                for device in self
                if self._identities[id(device)].serial_number == serial_number
            ),
            None,
        )


def accept_tcp_device(
    device: type[D],
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    device_id: int | None = None,
    device_name: str | None = None,
    serial_number: int | None = None,
    window: float = 25.0,
    max_candidates: int = 8,
    timeout: float = DEFAULT_TIMEOUT,
    recv_size: int = DEFAULT_RECV_SIZE,
    reply_timeout: float | None = None,
    raise_on_error: bool = True,
    setup_cb: SetupCallback | None = None,
) -> D:
    """Build ``device`` over an accepted TCP connection and open it.

    The Python host listens on ``host`` and ``port`` while ESP32 devices connect
    outward to it. Candidates may be selected by ``WhoAmI``, device-name prefix,
    and/or serial number. Non-matching or invalid candidates are closed while the
    listener continues until ``window`` or ``max_candidates`` is reached.

    Like :func:`harp.serial.open_serial_device`, the returned device is already
    open; use it directly or in a ``with`` block for guaranteed close.
    """
    if window <= 0:
        raise ValueError("window must be greater than zero")
    if max_candidates <= 0:
        raise ValueError("max_candidates must be greater than zero")
    _validate_options(timeout, recv_size, reply_timeout)

    deadline = time.monotonic() + window
    filters_enabled = any(
        value is not None for value in (device_id, device_name, serial_number)
    )

    with _open_server(host, port) as server:
        for attempt in range(1, max_candidates + 1):
            conn, peer = _accept_until(server, deadline, host, port)
            if setup_cb is not None:
                try:
                    setup_cb(conn)
                except Exception:
                    conn.close()
                    raise
            try:
                transport = TcpTransport(conn, timeout, recv_size)
                candidate = _open_device(device, transport, reply_timeout, raise_on_error)
            except Exception as exc:
                conn.close()
                _logger.warning(
                    "TCP candidate %s failed to open (attempt %d/%d): %s",
                    peer,
                    attempt,
                    max_candidates,
                    exc,
                )
                continue

            if not filters_enabled:
                return candidate

            try:
                identity = _read_identity(candidate, peer)
            except Exception as exc:
                _logger.warning("TCP identity read failed for %s: %s", peer, exc)
                candidate.close()
                continue

            if _matches(identity, device_id, device_name, serial_number):
                return candidate

            _logger.info(
                "Rejected TCP candidate %s (WhoAmI=%d, name=%r, serial=%d)",
                peer,
                identity.who_am_i,
                identity.name,
                identity.serial_number,
            )
            candidate.close()

    raise TimeoutError(
        f"No matching device connected after {max_candidates} candidate attempts"
    )


def accept_tcp_devices(
    device: type[D],
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    window: float = 10.0,
    timeout: float = DEFAULT_TIMEOUT,
    recv_size: int = DEFAULT_RECV_SIZE,
    reply_timeout: float | None = None,
    raise_on_error: bool = True,
    setup_cb: SetupCallback | None = None,
) -> TcpDeviceList[D]:
    """Accept all devices that connect during a bounded discovery window.

    Each accepted socket is probed concurrently so a slow or malformed candidate
    cannot prevent other ESP32 devices from connecting. Candidates that cannot be
    opened or do not answer the three identity-register reads are discarded.
    """
    if window <= 0:
        raise ValueError("window must be greater than zero")
    _validate_options(timeout, recv_size, reply_timeout)

    deadline = time.monotonic() + window
    results: list[tuple[D, TcpDeviceIdentity]] = []
    workers: list[tuple[threading.Thread, socket.socket]] = []
    results_lock = threading.Lock()

    def probe(conn: socket.socket, peer: str) -> None:
        candidate: D | None = None
        try:
            transport = TcpTransport(conn, timeout, recv_size)
            candidate = _open_device(device, transport, reply_timeout, raise_on_error)
            identity = _read_identity(candidate, peer)
            with results_lock:
                results.append((candidate, identity))
        except Exception as exc:
            _logger.warning("TCP discovery probe failed for %s: %s", peer, exc)
            if candidate is not None:
                candidate.close()
            else:
                conn.close()

    with _open_server(host, port) as server:
        while True:
            try:
                conn, peer = _accept_until(server, deadline, host, port)
            except TimeoutError:
                break

            try:
                if setup_cb is not None:
                    setup_cb(conn)
            except Exception:
                conn.close()
                raise

            worker = threading.Thread(
                target=probe,
                args=(conn, peer),
                daemon=True,
                name=f"harp-tcp-probe-{peer}",
            )
            workers.append((worker, conn))
            worker.start()

    effective_reply_timeout = (
        reply_timeout if reply_timeout is not None else device.REPLY_TIMEOUT
    )
    for worker, conn in workers:
        worker.join(timeout=effective_reply_timeout * 4)
        if worker.is_alive():
            conn.close()
            worker.join(timeout=1.0)

    results.sort(key=lambda entry: (entry[1].who_am_i, entry[1].serial_number))
    return TcpDeviceList(results)


def _open_device(
    device: type[D],
    transport: TcpTransport,
    reply_timeout: float | None,
    raise_on_error: bool,
) -> D:
    try:
        return device(
            transport,
            reply_timeout=reply_timeout,
            raise_on_error=raise_on_error,
        ).open()
    except Exception:
        transport.close()
        raise


def _validate_options(
    timeout: float,
    recv_size: int,
    reply_timeout: float | None,
) -> None:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if recv_size <= 0:
        raise ValueError("recv_size must be greater than zero")
    if reply_timeout is not None and reply_timeout <= 0:
        raise ValueError("reply_timeout must be greater than zero")


def _read_identity(device: Device, peer: str) -> TcpDeviceIdentity:
    return TcpDeviceIdentity(
        who_am_i=int(device.read(WhoAmI).parsed),
        name=device.read(DeviceName).parsed,
        serial_number=int(device.read(SerialNumber).parsed),
        peer=peer,
    )


def _matches(
    identity: TcpDeviceIdentity,
    device_id: int | None,
    device_name: str | None,
    serial_number: int | None,
) -> bool:
    return (
        (device_id is None or identity.who_am_i == device_id)
        and (
            device_name is None
            or identity.name.casefold().startswith(device_name.casefold())
        )
        and (serial_number is None or identity.serial_number == serial_number)
    )


def _open_server(host: str, port: int, backlog: int = 32) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(backlog)
        return server
    except OSError as exc:
        server.close()
        raise TransportError(f"Failed to listen on TCP endpoint {host}:{port}: {exc}") from exc


def _accept_until(
    server: socket.socket,
    deadline: float,
    host: str,
    port: int,
) -> tuple[socket.socket, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"No device connected to {host}:{port} within the discovery window"
        )

    server.settimeout(remaining)
    try:
        conn, address = server.accept()
    except socket.timeout as exc:
        raise TimeoutError(
            f"No device connected to {host}:{port} within the discovery window"
        ) from exc
    except OSError as exc:
        raise TransportError(f"Failed to accept a TCP connection: {exc}") from exc

    peer = f"{address[0]}:{address[1]}"
    return conn, peer


def _set_tcp_nodelay(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as exc:
        _logger.debug("Could not set TCP_NODELAY: %s", exc)
