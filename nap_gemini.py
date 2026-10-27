"""Bounded Gemini relay in the existing HTTP process. Never returns the key."""
import base64
import collections
import datetime
import hmac
import io
import wave
import ipaddress
import json
import stat
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

KEY_FILE = Path('/data/nap_private/gemini.key')
MODEL = 'gemini-3.5-flash-lite'
_lock = threading.Lock()
_requests = collections.deque()
_day = None
_daily = 0
_active = 0
_live_starts = collections.deque()


def read_key():
    info = KEY_FILE.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('Private key file permissions invalid')
    value = KEY_FILE.read_text().strip()
    if not 20 <= len(value) <= 256 or any(c.isspace() for c in value):
        raise ValueError('Private key file invalid')
    return value


def reserve(now=None):
    global _day, _daily, _active
    now = time.time() if now is None else now
    with _lock:
        day = int(now // 86400)
        if day != _day:
            _day, _daily = day, 0
        while _requests and now - _requests[0] >= 60:
            _requests.popleft()
        if len(_requests) >= 20 or _daily >= 300 or _active >= 2:
            return False
        _requests.append(now)
        _daily += 1
        _active += 1
        return True


def handle(handler, pair_token=None):
    global _active
    live = urlparse(handler.path).path == '/api/assistant/live-token'
    speech = urlparse(handler.path).path == '/api/assistant/speech'
    if urlparse(handler.path).path not in ('/api/assistant/chat', '/api/assistant/speech', '/api/assistant/live-token'):
        return False
    def reply(message, code):
        handler.send_json({'error': message}, code)
        return True
    # The key is shared only through this relay on the trusted local network.
    peer = ipaddress.ip_address(handler.client_address[0])
    trusted = peer.is_loopback or any(peer in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not trusted:
        return reply('Assistant is available only on the local network.', 403)
    origin = handler.headers.get('Origin')
    if origin and urlparse(origin).netloc != handler.headers.get('Host'):
        return reply('Same origin required.', 403)
    # Live tokens always require an already paired browser. Public HTTPS relay
    # access also requires pairing, even though Funnel forwards from loopback.
    host = urlparse('http://' + handler.headers.get('Host', '')).hostname or ''
    try:
        address = ipaddress.ip_address(host)
        local_host = address.is_loopback or any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except ValueError:
        local_host = host == 'localhost'
    supplied = handler.headers.get('X-NAP-Phone-Key', '')
    if (live or not local_host) and (not pair_token or not hmac.compare_digest(supplied, pair_token)):
        return reply('Pair this browser using Phone Navigation before starting the assistant on HTTPS.', 401)
    if handler.headers.get('X-NAP-Assistant') != '1' or handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
        return reply('Assistant JSON request required.', 415)
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 98304:
            return reply('Message too large.', 413)
        body = json.loads(handler.rfile.read(size))
        if live:
            if body != {}:
                return reply('Invalid live-session request.', 400)
            now = datetime.datetime.now(datetime.timezone.utc)
            body = {'uses': 1, 'expireTime': (now + datetime.timedelta(minutes=5)).isoformat(), 'newSessionExpireTime': (now + datetime.timedelta(seconds=60)).isoformat(), 'bidiGenerateContentSetup': {'model': 'models/gemini-3.8-live'}, 'fieldMask': 'model'}
        elif speech:
            if not isinstance(body, dict) or set(body) != {'text'} or not isinstance(body['text'], str) or not 1 <= len(body['text']) <= 1200:
                return reply('Voice text must be between 1 and 1200 characters.', 400)
            body = {'model': 'gemini-3.8-flash-lite-tts', 'input': [{'type': 'user_input', 'content': [{'type': 'text', 'text': body['text'], 'annotations': [{'type': 'speech_metadata', 'style': 'Warm, clear, natural conversational speech. Read units as complete words.'}]}]}], 'response_format': {'type': 'audio'}, 'generation_config': {'speech_config': [{'voice': 'Kore'}]}}
        else:
            if not isinstance(body, dict) or set(body) - {'contents', 'systemInstruction', 'tools', 'generationConfig'}:
                return reply('Invalid assistant request.', 400)
            contents = body.get('contents')
            if not isinstance(contents, list) or not 1 <= len(contents) <= 32:
                return reply('Conversation too long. Start a new conversation.', 400)
            body['generationConfig'] = {'maxOutputTokens': 1536}
        key = read_key()
    except (OSError, ValueError, TypeError):
        return reply('Assistant configuration or request unavailable.', 400)
    if live:
        with _lock:
            now = time.time()
            while _live_starts and now - _live_starts[0] >= 86400:
                _live_starts.popleft()
            if len(_live_starts) >= 12 or (_live_starts and now - _live_starts[-1] < 10):
                return reply('Live conversation start limit reached. Text chat is still available.', 429)
            _live_starts.append(now)
    if not reserve():
        return reply('Assistant request limit reached. Please try later.', 429)
    try:
        endpoint = 'https://generativelanguage.googleapis.com/v1beta/auth_tokens' if live else 'https://generativelanguage.googleapis.com/v1beta/interactions' if speech else 'https://generativelanguage.googleapis.com/v1beta/models/' + MODEL + ':generateContent'
        request = Request(endpoint,
                          data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'x-goog-api-key': key}, method='POST')
        with urlopen(request, timeout=22) as upstream:
            limit = 4 * 1024 * 1024 if speech else 1024 * 1024
            raw = upstream.read(limit + 1)
        if len(raw) > limit:
            return reply('Gemini response too large.', 502)
        result = json.loads(raw)
        # A credential must never be relayed even if echoed by the upstream.
        if key in json.dumps(result):
            return reply('Gemini response could not be displayed.', 502)
        if live:
            name = result.get('name')
            if not isinstance(name, str) or not name:
                return reply('Live session token unavailable.', 502)
            handler.send_json({'name': name, 'model': 'models/gemini-3.8-live', 'max_seconds': 300})
            return True
        if speech:
            audio = [c for step in result.get('steps', []) if step.get('type') == 'model_output' for c in step.get('content', []) if c.get('type') == 'audio']
            if not audio:
                return reply('Gemini returned no voice audio.', 502)
            item = audio[-1]
            data = base64.b64decode(item.get('data', ''), validate=True)
            if not data or len(data) > 3 * 1024 * 1024:
                return reply('Voice audio unavailable or too large.', 502)
            if not (data.startswith(b'RIFF') and data[8:12] == b'WAVE'):
                mime = item.get('mime_type', item.get('mimeType', ''))
                if not mime.startswith('audio/L16'):
                    return reply('Unsupported voice audio format.', 502)
                output = io.BytesIO()
                with wave.open(output, 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000); wav.writeframes(data)
                data = output.getvalue()
            handler.send_bytes(data, 'audio/wav')
            return True
        handler.send_json(result)
        return True
    except HTTPError as exc:
        return reply('Gemini quota reached.' if exc.code == 429 else 'Gemini request failed. Check server configuration.', 429 if exc.code == 429 else 502)
    except (URLError, TimeoutError, OSError, ValueError):
        return reply('Gemini connection unavailable. Please try again.', 502)
    finally:
        with _lock:
            _active -= 1
