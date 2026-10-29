"""Papra custom-http extraction -> local Ollama vision OCR.

No Papra credentials, database access, or persistent document storage needed.
"""
import base64
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import warnings

import pymupdf
from flask import Flask, jsonify, request
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.exceptions import HTTPException

# Decode the full-resolution HEIC image before our normal OCR resizing.
register_heif_opener(thumbnails=False)

app = Flask(__name__)
MAX_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', '26214400'))
app.config['MAX_CONTENT_LENGTH'] = MAX_BYTES + 1024 * 1024  # multipart overhead
app.config['MAX_FORM_MEMORY_SIZE'] = 1024 * 1024
OLLAMA = os.getenv('OLLAMA_URL', 'http://ollama:11434').rstrip('/')
MODEL = os.getenv('OCR_MODEL', 'glm-ocr:q8_0')
PROMPT = os.getenv('OCR_PROMPT', 'Text Recognition:')
MAX_PAGES = int(os.getenv('OCR_MAX_PAGES', '50'))
MAX_EDGE = int(os.getenv('OCR_MAX_EDGE', '2500'))
DPI = int(os.getenv('OCR_DPI', '200'))
DOC_TIMEOUT = int(os.getenv('OCR_DOCUMENT_TIMEOUT_SECONDS', '840'))
PAGE_TIMEOUT = int(os.getenv('OCR_PAGE_TIMEOUT_SECONDS', '180'))
Image.MAX_IMAGE_PIXELS = 80_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
gate = threading.BoundedSemaphore(1)
log = logging.getLogger('gunicorn.error')


class ExtractionError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ExtractionError('Document OCR deadline exceeded', 504)
    return seconds


def png_bytes(image):
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def pages(data, mime):
    if mime == 'application/pdf' or data.lstrip().startswith(b'%PDF-'):
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            if doc.needs_pass:
                raise ExtractionError('Password-protected PDF requires decryption first')
            if not 0 < doc.page_count <= MAX_PAGES:
                raise ExtractionError('PDF is empty or exceeds OCR_MAX_PAGES')
            for page in doc:
                # Bound dimensions before rasterizing, including oversize PDF pages.
                scale = min(DPI / 72, MAX_EDGE / max(page.rect.width, page.rect.height))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                yield pix.tobytes('png')
    else:
        with Image.open(io.BytesIO(data)) as source:
            if not 0 < getattr(source, 'n_frames', 1) <= MAX_PAGES:
                raise ExtractionError('Image is empty or exceeds OCR_MAX_PAGES')
            if source.format == 'HEIF':
                # A phone photo may contain alternate images. OCR its primary
                # photo once; keep TIFF's multi-page behavior below unchanged.
                for index in range(source.n_frames):
                    source.seek(index)
                    if source.info.get('primary', False):
                        yield png_bytes(source.copy())
                        return
                source.seek(0)
                yield png_bytes(source.copy())
                return
            for index in range(getattr(source, 'n_frames', 1)):
                source.seek(index)
                yield png_bytes(source.copy())


def recognize(image, deadline):
    payload = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': PROMPT,
                      'images': [base64.b64encode(image).decode('ascii')]}],
        'stream': False,
        'keep_alive': os.getenv('OCR_KEEP_ALIVE', '1m'),
        'options': {
            'temperature': 0,
            'num_ctx': int(os.getenv('OCR_CONTEXT', '8192')),
            'num_predict': int(os.getenv('OCR_MAX_OUTPUT_TOKENS', '4096')),
        },
    }
    # Do not send 'think' for GLM-OCR: it is a dedicated recognition model.
    if MODEL.startswith('qwen3'):
        payload['think'] = False
    req = urllib.request.Request(OLLAMA + '/api/chat',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=min(PAGE_TIMEOUT, remaining(deadline))) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ExtractionError(f'Ollama returned HTTP {exc.code}; check model installation and Ollama logs', 502) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ExtractionError('Ollama is unreachable or page inference timed out', 502) from None
    except (ValueError, UnicodeError):
        raise ExtractionError('Ollama returned invalid JSON', 502) from None
    remaining(deadline)
    if not isinstance(result, dict) or result.get('error') or not result.get('done'):
        raise ExtractionError('Ollama did not complete inference', 502)
    if result.get('done_reason') == 'length':
        raise ExtractionError('OCR output was truncated; increase context/output limits or split the document', 502)
    message = result.get('message')
    text = message.get('content') if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise ExtractionError('Ollama did not return text', 502)
    return text.strip()


@app.get('/health')
def health():
    # Liveness only: model availability is checked by actual extraction.
    return jsonify(status='ok')


@app.post('/extract')
def extract():
    deadline = time.monotonic() + DOC_TIMEOUT
    upload = request.files.get('file')
    if upload is None:
        raise ExtractionError('Expected multipart upload in the file field', 400)
    data = upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ExtractionError('Upload is empty or exceeds MAX_UPLOAD_BYTES', 413)
    # Keep concurrency bounded; failed documents can be retried via reprocessing.
    if not gate.acquire(timeout=min(30, remaining(deadline))):
        raise ExtractionError('OCR adapter busy; retry using Papra document reprocessing', 503)
    try:
        texts = []
        for page in pages(data, upload.mimetype):
            remaining(deadline)
            texts.append(recognize(page, deadline))
        if not any(texts):
            raise ExtractionError('OCR returned no text')
        text = '\n\n'.join(texts)
        log.info('OCR completed: model=%s pages=%d', MODEL, len(texts))
        return jsonify(text=text)
    except ExtractionError:
        raise
    except (pymupdf.FileDataError, UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ExtractionError('Unsupported, damaged, or oversized PDF/image') from None
    finally:
        gate.release()


@app.errorhandler(ExtractionError)
def extraction_error(exc):
    # Never log filenames, image data, prompts, or recognized document contents.
    log.warning('OCR failed: %s', exc)
    return jsonify(error=str(exc)), exc.status


@app.errorhandler(HTTPException)
def http_error(exc):
    return jsonify(error=exc.description), exc.code
