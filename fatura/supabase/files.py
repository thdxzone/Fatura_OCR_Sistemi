"""Fatura PDF'leri için Supabase Storage istemcisi (private bucket).

Yalnızca backend kullanır; service_role anahtarı ortam değişkeninden gelir ve hiçbir
hata mesajına veya loga yazılmaz. Ek bağımlılık gerektirmemek için urllib kullanılır.
"""

import json
from pathlib import Path
import tempfile
import urllib.error
from urllib.parse import quote
import urllib.request


class StorageError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP durum kodu; bağlantı hatasında None


class SupabaseFiles:
    def __init__(self, url, service_key, bucket="fatura-belgeler", timeout=60):
        if not url or not service_key:
            raise ValueError("SUPABASE_URL ve SUPABASE_SERVICE_ROLE_KEY tanımlanmalı.")
        self.base = url.rstrip("/") + "/storage/v1/object"
        self.bucket = bucket
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def __repr__(self):
        return f"SupabaseFiles(bucket={self.bucket!r})"

    def _url(self, path=""):
        return f"{self.base}/{quote(self.bucket)}" + (f"/{quote(path)}" if path else "")

    def _request(self, method, url, body=None, headers=None):
        request = urllib.request.Request(
            url, data=body, method=method, headers=self._headers | (headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            # Yanıt gövdesi yalnızca kısa bir özet olarak alınır; istek başlıkları yazılmaz.
            detail = error.read(300).decode("utf-8", "replace")
            raise StorageError(f"Belge deposu {method} {error.code}: {detail}", error.code) from None
        except urllib.error.URLError as error:
            raise StorageError(f"Belge deposuna ulaşılamadı: {error.reason}") from None

    def upload(self, path, data):
        """Var olan nesnenin üzerine yazmaz (x-upsert: false)."""
        self._request(
            "POST",
            self._url(path),
            data,
            {"Content-Type": "application/pdf", "x-upsert": "false"},
        )

    def download(self, path, destination):
        """Nesneyi yerel önbelleğe atomik olarak yazar (yarım dosya kalmaz)."""
        data = self._request("GET", self._url(path))
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            handle.write(data)
        Path(handle.name).replace(destination)
        return destination

    def delete(self, paths):
        if paths:
            self._request(
                "DELETE",
                self._url(),
                json.dumps({"prefixes": list(paths)}).encode(),
                {"Content-Type": "application/json"},
            )
