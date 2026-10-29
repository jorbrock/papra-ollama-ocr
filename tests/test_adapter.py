"""Transport/rendering tests: no real model or NAS required.

From ocr-adapter/: python -m unittest discover -s tests -v
"""
import base64
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pymupdf
from PIL import Image

spec = importlib.util.spec_from_file_location('adapter', Path(__file__).parents[1] / 'app.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class FakeOllama(BaseHTTPRequestHandler):
    received = []
    response_kind = 'ok'

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).received.append((self.path, payload))
        page = Image.open(io.BytesIO(base64.b64decode(payload['messages'][0]['images'][0])))
        page.verify()  # Prove the adapter sent a valid raster, not PDF bytes.
        if type(self).response_kind == 'error':
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"private model detail"}')
            return
        kind = type(self).response_kind
        text = '' if kind == 'empty' else f'Recognized page {len(type(self).received)}'
        result = {'message': {'content': text}, 'done': True,
                  'done_reason': 'length' if kind == 'truncated' else 'stop'}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, *args):
        pass


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        adapter.OLLAMA = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FakeOllama.received = []
        FakeOllama.response_kind = 'ok'
        self.client = adapter.app.test_client()

    def png(self):
        buf = io.BytesIO()
        Image.new('RGB', (240, 120), 'white').save(buf, 'PNG')
        return buf.getvalue()

    def pdf(self, count=2):
        with pymupdf.open() as doc:
            for n in range(count):
                doc.new_page().insert_text((72, 72), f'Document page {n + 1}')
            return doc.tobytes()

    def upload(self, data, name, mime):
        return self.client.post('/extract', data={'file': (io.BytesIO(data), name, mime)})

    def test_pdf_page_order_and_native_api_contract(self):
        result = self.upload(self.pdf(), 'two-pages.pdf', 'application/pdf')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['text'], 'Recognized page 1\n\nRecognized page 2')
        self.assertEqual(len(FakeOllama.received), 2)
        path, body = FakeOllama.received[0]
        self.assertEqual(path, '/api/chat')
        self.assertFalse(body['stream'])
        self.assertEqual(body['messages'][0]['content'], 'Text Recognition:')
        self.assertNotIn('think', body)
        image = Image.open(io.BytesIO(base64.b64decode(body['messages'][0]['images'][0])))
        self.assertLessEqual(max(image.size), adapter.MAX_EDGE + 1)

    def test_png_and_qwen_switch(self):
        with patch.object(adapter, 'MODEL', 'qwen3-vl:8b'):
            result = self.upload(self.png(), 'image.png', 'image/png')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(FakeOllama.received[0][1]['think'])

    def test_rotated_jpeg(self):
        image = Image.new('RGB', (240, 120), 'white')
        exif = image.getexif()
        exif[274] = 6
        buf = io.BytesIO()
        image.save(buf, format='JPEG', exif=exif)
        result = self.upload(buf.getvalue(), 'rotated.jpg', 'image/jpeg')
        self.assertEqual(result.status_code, 200)
        encoded = FakeOllama.received[0][1]['messages'][0]['images'][0]
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(encoded))).size, (120, 240))

    def test_multipage_tiff(self):
        buf = io.BytesIO()
        Image.new('RGB', (120, 120), 'white').save(
            buf, format='TIFF', save_all=True,
            append_images=[Image.new('RGB', (120, 120), 'gray')])
        self.assertEqual(self.upload(buf.getvalue(), 'scan.tiff', 'image/tiff').status_code, 200)
        self.assertEqual(len(FakeOllama.received), 2)

    def test_heic_and_heif_decode_to_png(self):
        buf = io.BytesIO()
        Image.new('RGB', (240, 120), 'white').save(buf, format='HEIF', quality=90)
        for name, mime in [('iphone.heic', 'image/heic'), ('photo.heif', 'image/heif')]:
            with self.subTest(mime=mime):
                result = self.upload(buf.getvalue(), name, mime)
                self.assertEqual(result.status_code, 200)
                encoded = FakeOllama.received[-1][1]['messages'][0]['images'][0]
                image = Image.open(io.BytesIO(base64.b64decode(encoded)))
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, (240, 120))

    def test_heic_primary_photo_only(self):
        buf = io.BytesIO()
        Image.new('RGB', (240, 120), 'red').save(
            buf, format='HEIF', save_all=True, primary_index=1, quality=100,
            append_images=[Image.new('RGB', (120, 240), 'white')])
        result = self.upload(buf.getvalue(), 'multiple.heic', 'image/heic')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(FakeOllama.received), 1)
        encoded = FakeOllama.received[0][1]['messages'][0]['images'][0]
        image = Image.open(io.BytesIO(base64.b64decode(encoded)))
        self.assertEqual(image.size, (120, 240))
        self.assertTrue(all(channel > 240 for channel in image.getpixel((60, 120))))

    def test_heic_exif_orientation(self):
        image = Image.new('RGB', (240, 120), 'white')
        exif = image.getexif()
        exif[274] = 6
        buf = io.BytesIO()
        # Serialized EXIF lets the encoder write matching HEIF rotation metadata.
        image.save(buf, format='HEIF', exif=exif.tobytes(), quality=90)
        result = self.upload(buf.getvalue(), 'rotated.heic', 'image/heic')
        self.assertEqual(result.status_code, 200)
        encoded = FakeOllama.received[0][1]['messages'][0]['images'][0]
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(encoded))).size, (120, 240))

    def test_damaged_heic_returns_error_and_recovers(self):
        result = self.upload(b'not a HEIC image', 'broken.heic', 'image/heic')
        self.assertEqual(result.status_code, 422)
        self.assertEqual(FakeOllama.received, [])
        self.assertEqual(self.upload(self.png(), 'next.png', 'image/png').status_code, 200)

    def test_failed_upstream_returns_error_and_next_document_can_run(self):
        FakeOllama.response_kind = 'error'
        result = self.upload(self.png(), 'image.png', 'image/png')
        self.assertEqual(result.status_code, 502)
        self.assertNotIn('private model detail', result.json['error'])
        FakeOllama.response_kind = 'ok'
        self.assertEqual(self.upload(self.png(), 'image.png', 'image/png').status_code, 200)

    def test_truncated_and_empty_responses_are_not_success(self):
        for kind, expected in [('truncated', 502), ('empty', 422)]:
            FakeOllama.response_kind = kind
            self.assertEqual(self.upload(self.png(), 'image.png', 'image/png').status_code, expected)

    def test_encrypted_pdf_and_page_limit(self):
        with pymupdf.open(stream=self.pdf(), filetype='pdf') as doc:
            encrypted = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                   owner_pw='owner', user_pw='reader')
        self.assertEqual(self.upload(encrypted, 'locked.pdf', 'application/pdf').status_code, 422)
        with patch.object(adapter, 'MAX_PAGES', 1):
            self.assertEqual(self.upload(self.pdf(), 'large.pdf', 'application/pdf').status_code, 422)
        self.assertEqual(FakeOllama.received, [])

    def test_missing_invalid_oversize_and_expired(self):
        self.assertEqual(self.client.post('/extract').status_code, 400)
        self.assertEqual(self.upload(b'bad file', 'bad.pdf', 'application/pdf').status_code, 422)
        with patch.object(adapter, 'MAX_BYTES', 5):
            self.assertEqual(self.upload(self.png(), 'large.png', 'image/png').status_code, 413)
        with patch.object(adapter, 'DOC_TIMEOUT', -1):
            self.assertEqual(self.upload(self.png(), 'image.png', 'image/png').status_code, 504)

    def test_health_does_not_require_ocr_slot(self):
        adapter.gate.acquire()
        try:
            self.assertEqual(self.client.get('/health').json, {'status': 'ok'})
        finally:
            adapter.gate.release()


if __name__ == '__main__':
    unittest.main()
