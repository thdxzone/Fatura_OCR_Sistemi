"""Container içinden üretim HTTP yollarını parolayı yazdırmadan kontrol eder."""

import base64
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def main():
    public = urlsplit(os.environ["FATURA_PUBLIC_URL"])
    port = os.environ.get("PORT", "8765")
    user, password = (
        os.environ["FATURA_ADMIN_USER"],
        os.environ["FATURA_ADMIN_PASSWORD"],
    )
    token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def request(path, auth=False, host=public.netloc):
        headers = {"Host": host}
        if auth:
            headers["Authorization"] = "Basic " + token
        req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    assert request("/health", host=f"127.0.0.1:{port}")[0] == 200
    assert request("/api/documents")[0] == 401
    assert request("/", auth=True)[0] == 200
    status, body = request("/api/documents", auth=True)
    assert status == 200
    assert "documents" in json.loads(body)
    assert request("/api/documents", auth=True, host="evil.example")[0] == 403
    print("PASS: sağlık, giriş, HTML, fatura API ve Host kontrolü")


if __name__ == "__main__":
    main()
