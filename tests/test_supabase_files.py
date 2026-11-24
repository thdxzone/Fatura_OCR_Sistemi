"""Supabase Storage istemcisinin HTTP isteklerini sahte bir Storage sunucusuyla sınar."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fatura.supabase.files import StorageError, SupabaseFiles

KEY = "test-anahtari-gizli"


class FakeStorage(BaseHTTPRequestHandler):
    objects = {}
    requests = []

    def log_message(self, *args):
        pass

    def record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # HTTP başlık adları büyük/küçük harfe duyarsızdır.
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.requests.append((self.command, self.path, headers, body))
        return body

    def reply(self, status, body=b"{}"):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}" and self.headers.get("apikey") == KEY

    def do_POST(self):
        body = self.record()
        if not self.authorized():
            return self.reply(403, b'{"error":"yetkisiz"}')
        if self.path in self.objects and self.headers.get("x-upsert") != "true":
            return self.reply(409, b'{"error":"Duplicate","message":"The resource already exists"}')
        self.objects[self.path] = body
        self.reply(200, b'{"Key":"ok"}')

    def do_GET(self):
        self.record()
        if self.path not in self.objects:
            return self.reply(404, b'{"error":"not_found"}')
        self.reply(200, self.objects[self.path])

    def do_DELETE(self):
        body = json.loads(self.record())
        bucket = self.path
        for prefix in body["prefixes"]:
            self.objects.pop(f"{bucket}/{prefix}", None)
        self.reply(200, b"[]")


class SupabaseFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStorage)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeStorage.objects.clear()
        FakeStorage.requests.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.files = SupabaseFiles(self.url, KEY)

    def test_upload_download_delete(self):
        path = "2026/11/0f044f36-22a2-4dc1-b003-5f85217337ca.pdf"
        self.files.upload(path, b"%PDF-1.4 ornek")
        method, url, headers, _ = FakeStorage.requests[0]
        self.assertEqual((method, url), ("POST", f"/storage/v1/object/fatura-belgeler/{path}"))
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertEqual(headers["x-upsert"], "false")

        target = self.files.download(path, self.temp / "cache" / "source")
        self.assertEqual(target.read_bytes(), b"%PDF-1.4 ornek")
        self.assertEqual(list((self.temp / "cache").iterdir()), [target])  # geçici dosya kalmaz

        self.files.delete([path])
        self.assertEqual(FakeStorage.objects, {})

    def test_existing_object_is_not_overwritten(self):
        self.files.upload("2026/11/a.pdf", b"ilk")
        with self.assertRaisesRegex(StorageError, "409"):
            self.files.upload("2026/11/a.pdf", b"ikinci")
        self.assertEqual(FakeStorage.objects["/storage/v1/object/fatura-belgeler/2026/11/a.pdf"], b"ilk")

    def test_errors_do_not_leak_the_key(self):
        wrong = SupabaseFiles(self.url, "yanlis-anahtar")
        with self.assertRaises(StorageError) as raised:
            wrong.upload("2026/11/a.pdf", b"x")
        self.assertNotIn("yanlis-anahtar", str(raised.exception))
        self.assertNotIn(KEY, repr(self.files))
        with self.assertRaisesRegex(StorageError, "404"):
            self.files.download("yok.pdf", self.temp / "x")
        self.assertFalse((self.temp / "x").exists())
        with self.assertRaisesRegex(StorageError, "ulaşılamadı"):
            SupabaseFiles("http://127.0.0.1:9", KEY, timeout=2).download("a.pdf", self.temp / "y")

    def test_missing_configuration_fails_fast(self):
        with self.assertRaises(ValueError):
            SupabaseFiles("", KEY)
        with self.assertRaises(ValueError):
            SupabaseFiles(self.url, None)


if __name__ == "__main__":
    unittest.main()
