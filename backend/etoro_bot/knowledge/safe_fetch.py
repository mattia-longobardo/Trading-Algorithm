"""Fetch HTTP con difesa da SSRF, per gli URL che decide l'utente.

I feed RSS sono configurabili dalla UI e il backend li scarica *lui*, dall'interno
della rete Docker: senza controlli un URL come `http://trading-postgres:5432/` o
`http://169.254.169.254/` diventerebbe una primitiva di lettura sulla rete
interna, per giunta con il risultato indicizzato nella knowledge base e quindi
leggibile dalla pagina News.

La difesa è sulla *destinazione risolta*, non sulla stringa: si risolve il
nome, si scartano gli indirizzi non pubblici e si ripete il controllo a ogni
redirect (un 302 verso 127.0.0.1 aggirerebbe un controllo fatto solo all'inizio).

Risolvere e poi lasciare che sia urlopen a risolvere di nuovo lascerebbe però
aperta la finestra del DNS rebinding: la seconda risoluzione può rispondere
127.0.0.1. Per questo la connessione viene aperta verso l'INDIRIZZO già
validato, conservando l'hostname per l'header Host e per l'SNI/verifica del
certificato TLS.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

FETCH_TIMEOUT_S = 10
MAX_REDIRECTS = 3
MAX_BYTES = 5_000_000


class UnsafeUrlError(ValueError):
    """URL che punta fuori dalla rete pubblica: rifiutato prima di connettersi."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str) -> str:
    """Rifiuta schemi diversi da http(s) e host che risolvono fuori da Internet.

    Ritorna l'indirizzo IP validato: è quello a cui il fetch si connetterà
    davvero, così fra il controllo e la connessione non c'è una seconda
    risoluzione DNS da poter avvelenare. Tutti i record devono essere
    pubblici: ne basta uno interno perché il round-robin del DNS possa
    portarci sulla rete interna.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"schema non ammesso: {url}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(f"URL senza host: {url}")

    try:
        resolved = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"host non risolvibile: {host}") from exc

    addresses = [info[4][0] for info in resolved]
    if not addresses:
        raise UnsafeUrlError(f"host non risolvibile: {host}")
    for address in addresses:
        if not _is_public(address):
            raise UnsafeUrlError(f"host non pubblico: {host} → {address}")
    return addresses[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connessione all'IP già validato, con `self.host` intatto per l'header Host."""

    pinned_ip: str | None = None

    def _create_connection(self, address, timeout, source_address):
        host, port = address
        return socket.create_connection(
            (self.pinned_ip or host, port), timeout, source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Come sopra: l'SNI e la verifica del certificato restano sull'hostname.

    HTTPSConnection.connect() passa `server_hostname=self.host` a wrap_socket,
    e self.host resta il nome: cambiamo solo il socket TCP sottostante.
    """

    pinned_ip: str | None = None

    def _create_connection(self, address, timeout, source_address):
        host, port = address
        return socket.create_connection(
            (self.pinned_ip or host, port), timeout, source_address
        )


def _connection_factory(ip: str | None, secure: bool):
    def build(host, **kwargs):
        cls = _PinnedHTTPSConnection if secure else _PinnedHTTPConnection
        connection = cls(host, **kwargs)
        connection.pinned_ip = ip
        return connection

    return build


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, ip: str | None) -> None:
        super().__init__()
        self._ip = ip

    def http_open(self, req):
        return self.do_open(_connection_factory(self._ip, False), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ip: str | None) -> None:
        super().__init__()
        self._ip = ip

    def https_open(self, req):
        return self.do_open(
            _connection_factory(self._ip, True), req, context=self._context
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """I redirect li seguiamo a mano, per poter rivalidare ogni tappa."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_text(url: str, *, user_agent: str, timeout: int = FETCH_TIMEOUT_S) -> str:
    """Scarica `url` come testo, validando l'URL iniziale e ogni redirect.

    L'IP validato è anche quello a cui ci si connette (connection pinning):
    fra il controllo e la connessione non c'è una seconda risoluzione DNS da
    poter avvelenare. Host header e SNI restano il nome originale.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ip = assert_public_url(current)
        opener = urllib.request.build_opener(
            _NoRedirect, _PinnedHTTPHandler(ip), _PinnedHTTPSHandler(ip)
        )
        request = urllib.request.Request(current, headers={"User-Agent": user_agent})
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read(MAX_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code not in (301, 302, 303, 307, 308) or not location:
                raise
            current = urljoin(current, location)
    raise UnsafeUrlError(f"troppi redirect: {url}")
