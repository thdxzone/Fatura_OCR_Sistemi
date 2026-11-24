"""Coolify için kimlik doğrulamalı WSGI uygulaması ve tek süreçli başlatıcı."""

import base64
import binascii
from dataclasses import dataclass, field
from email.message import Message
import hashlib
import hmac
from http import HTTPStatus
import io
import ipaddress
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from fatura.paths import PROJECT_ROOT
from fatura.web.app import Application, InvoiceEndpoints, MAX_UPLOAD

LOG = logging.getLogger("fatura.production")


def private_host(host):
    """HTTP istisnası yalnızca gerçek iç ağ/loopback IP'leri içindir; DNS çözülmez."""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    return address.is_loopback or any(
        address in ipaddress.ip_network(net) for net in networks
    )


@dataclass(frozen=True)
class Settings:
    public_url: str
    username: str
    password: str = field(repr=False)
    data_dir: Path = Path("/data")
    port: int = 8765
    allow_private_http: bool = False

    def __post_init__(self):
        url = urlsplit(self.public_url)
        if (
            (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and self.allow_private_http
                    and private_host(url.hostname)
                )
            )
            or not url.hostname
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or any(character.isspace() for character in self.public_url)
        ):
            raise ValueError(
                "FATURA_PUBLIC_URL HTTPS olmalı; iç ağ HTTP için özel IP ve FATURA_ALLOW_PRIVATE_HTTP=true gerekli."
            )
        if not self.username or ":" in self.username or len(self.password) < 16:
            raise ValueError(
                "Yönetici adı ve en az 16 karakterli FATURA_ADMIN_PASSWORD gerekli."
            )
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT 1–65535 aralığında olmalı.")
        # Geçersiz port ve boş alan adı da başlangıçta yakalanır.
        url.port

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("FATURA_PUBLIC_URL", "").rstrip("/"),
            os.environ.get("FATURA_ADMIN_USER", ""),
            os.environ.get("FATURA_ADMIN_PASSWORD", ""),
            Path(os.environ.get("FATURA_DATA_DIR", "/data")),
            int(os.environ.get("PORT", "8765")),
            os.environ.get("FATURA_ALLOW_PRIVATE_HTTP", "false").lower() == "true",
        )

    @property
    def authority(self):
        url = urlsplit(self.public_url)
        return url.netloc.lower().removesuffix(
            ":443" if url.scheme == "https" else ":80"
        )

    def trusted(self, headers, method, path):
        host = headers.get("Host", "").lower()
        default_port = ":443" if urlsplit(self.public_url).scheme == "https" else ":80"
        public = host.removesuffix(default_port) == self.authority
        # Sağlık kontrolü localhost üzerinden, parola göndermeden çalışabilir.
        health = path == "/health" and method in ("GET", "HEAD")
        local = host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if not public and not (health and local):
            return False
        origin = headers.get("Origin")
        expected = self.public_url.rstrip("/").lower().removesuffix(default_port)
        if origin and origin.lower().removesuffix(default_port) != expected:
            return False
        return headers.get("Sec-Fetch-Site") != "cross-site"

    def authenticated(self, header):
        try:
            scheme, encoded = (header or "").split(" ", 1)
            if scheme.lower() != "basic":
                return False
            user, password = (
                base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            )
        except (ValueError, UnicodeError, binascii.Error):
            return False

        # Sabit uzunluklu özetler karşılaştırılır; parola günlük veya yanıta yazılmaz.
        def digest(text):
            return hashlib.sha256(text.encode("utf-8")).digest()

        return hmac.compare_digest(
            digest(user), digest(self.username)
        ) & hmac.compare_digest(digest(password), digest(self.password))


class WSGIRequest(InvoiceEndpoints):
    """Paylaşılan uçları soket açmadan, Waitress'in WSGI isteğine uyarlar."""

    def __init__(self, application, settings, environ):
        self.server = SimpleNamespace(
            application=application, server_port=settings.port
        )
        self.settings = settings
        self.environ = environ
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                self.headers[key[5:].replace("_", "-")] = value
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(key):
                self.headers[key.replace("_", "-")] = environ[key]
        self.path = quote(environ.get("PATH_INFO", "/"), safe="/")
        if environ.get("QUERY_STRING"):
            self.path += "?" + environ["QUERY_STRING"]
        self.rfile = environ["wsgi.input"]
        self.wfile = io.BytesIO()
        self.status = 200
        self.response_headers = []

    def trusted(self):
        return self.settings.trusted(
            self.headers,
            self.environ["REQUEST_METHOD"],
            self.environ.get("PATH_INFO", "/"),
        )

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, str(value)))

    def end_headers(self):
        pass


class ProductionApp:
    def __init__(self, application, settings):
        self.application = application
        self.settings = settings

    def __call__(self, environ, start_response):
        request = WSGIRequest(self.application, self.settings, environ)
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        try:
            if not request.trusted():
                request.send(
                    403, {"error": "Alan adı veya istek kaynağı izinli değil."}
                )
            elif path == "/health" and method in ("GET", "HEAD"):
                request.send(200, {"status": "ok"})
            elif not self.settings.authenticated(request.headers.get("Authorization")):
                request.send(
                    401,
                    {"error": "Giriş yapmanız gerekiyor."},
                    extra={
                        "WWW-Authenticate": 'Basic realm="Fatura Sistemi", charset="UTF-8"'
                    },
                )
            elif method in ("GET", "HEAD"):
                request.do_GET()
            elif method == "POST":
                request.do_POST()
            else:
                request.send(
                    405,
                    {"error": "Bu yöntem desteklenmiyor."},
                    extra={"Allow": "GET, HEAD, POST"},
                )
        except Exception:
            LOG.exception("Üretim isteği tamamlanamadı")
            request.wfile = io.BytesIO()
            request.response_headers = []
            request.send(500, {"error": "İşlem tamamlanamadı."})
        request.response_headers.append(("X-Robots-Tag", "noindex, nofollow"))
        if urlsplit(self.settings.public_url).scheme == "https":
            request.response_headers.append(
                ("Strict-Transport-Security", "max-age=31536000")
            )
        start_response(
            f"{request.status} {HTTPStatus(request.status).phrase}",
            request.response_headers,
        )
        return [b"" if method == "HEAD" else request.wfile.getvalue()]


class InstanceLock:
    """Aynı SQLite diskiyle iki uygulama/rolling deployment başlatılmasını engeller."""

    def __init__(self, root):
        import fcntl

        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root / ".instance.lock").open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise RuntimeError(
                "Bu veri diskiyle başka bir uygulama çalışıyor. Önce eski container durmalı."
            )

    def close(self):
        self.stream.close()


def main():
    from waitress import create_server
    import resource

    # Native OCR hatasında belge/bellek içeriği içeren büyük core dosyası yazılmaz.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = Settings.from_env()
    lock = InstanceLock(settings.data_dir)
    application = None
    server = None
    try:
        # Sağlıklı görünmeden önce iki OCR modeli de gerçek, yapay bir metinle denenir.
        subprocess.run(
            [sys.executable, "-m", "scripts.ocr_smoke"],
            cwd=PROJECT_ROOT,
            check=True,
            timeout=120,
        )
        application = Application(settings.data_dir, reader="paddle")
        server = create_server(
            ProductionApp(application, settings),
            host="0.0.0.0",
            port=settings.port,
            threads=8,
            max_request_body_size=MAX_UPLOAD,
            channel_timeout=120,
            clear_untrusted_proxy_headers=True,
        )

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        LOG.info("Fatura üretim sunucusu hazır; port=%s", settings.port)
        server.run()
    finally:
        if server is not None:
            server.close()
        if application is not None:
            application.executor.shutdown(wait=True)
        lock.close()


if __name__ == "__main__":
    main()
