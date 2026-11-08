"""Real SSE transport helpers and bounded Telegram previews; no extra LLM calls."""
import asyncio
import contextlib
import contextvars
import json
import time
from collections import deque

CURRENT = contextvars.ContextVar('live_response', default=None)
MAX_STREAM_BYTES = 2_000_000


def events(response, stopped):
    """SSE comments, CRLF, multi-line data and bounded frames. Never parse reasoning."""
    parts, size, total = [], 0, 0
    deadline=time.monotonic()+120
    while not stopped.is_set():
        if time.monotonic()>deadline:raise TimeoutError('stream_deadline')
        raw = response.readline(262145)
        if not raw:
            break
        total += len(raw)
        if len(raw) > 262144 or total > MAX_STREAM_BYTES:
            raise ValueError('stream_too_large')
        line = raw.decode('utf-8').rstrip('\r\n')
        if not line:
            if parts:
                data = '\n'.join(parts)
                if data == '[DONE]':
                    yield data
                    return
                item = json.loads(data)
                if not isinstance(item, dict):
                    raise ValueError('invalid_stream_event')
                yield item
            parts, size = [], 0
        elif line.startswith('data:'):
            value = line[5:].removeprefix(' ')
            size += len(value)
            if size > 262144:
                raise ValueError('stream_frame_too_large')
            parts.append(value)
    # Missing DONE is ambiguous even when the answer happened to look complete.
    raise ValueError('incomplete_stream')


class Completion:
    def __init__(self):
        self.text, self.id, self.usage = '', '', {}
        self.finished = False

    def feed(self, event):
        if event == '[DONE]':
            if not self.finished:
                raise ValueError('incomplete_stream')
            return
        if isinstance(event.get('id'), str):
            self.id = event['id'][:200]
        if isinstance(event.get('usage'), dict):
            self.usage = event['usage']
        for choice in event.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            value = delta.get('content', '')
            if isinstance(value, list):
                value = ''.join(p.get('text', '') for p in value if isinstance(p, dict) and p.get('type') == 'text')
            if isinstance(value, str):
                self.text += value
            if choice.get('finish_reason'):
                if choice['finish_reason'] in {'error', 'length'}:
                    raise ValueError('incomplete_stream')
                self.finished = True

    def result(self):
        return {'id': self.id, 'usage': self.usage,
                'choices': [{'message': {'content': self.text}}]}


def answer_prefix(text):
    """Expose only a top-level JSON answer string, never memories/JSON/reasoning."""
    s = text.lstrip()
    if s.startswith('```'):
        if '\n' not in s:
            return ''
        s = s.split('\n', 1)[1].lstrip()
    if not s.startswith('{'):
        return ''  # Plain-text fallback is delivered once, after full validation.
    decoder, pos = json.JSONDecoder(), 1
    while pos < len(s):
        while pos < len(s) and s[pos] in ' \r\n\t,':
            pos += 1
        try:
            key, end = decoder.raw_decode(s, pos)
        except ValueError:
            return ''
        if not isinstance(key, str):
            return ''
        pos = end
        while pos < len(s) and s[pos].isspace():
            pos += 1
        if pos >= len(s) or s[pos] != ':':
            return ''
        pos += 1
        while pos < len(s) and s[pos].isspace():
            pos += 1
        if key != 'answer':
            try:
                _, pos = decoder.raw_decode(s, pos)
            except ValueError:
                return ''
            continue
        if pos >= len(s) or s[pos] != '"':
            return ''
        start, pos = pos + 1, pos + 1
        # Decode only complete escape sequences, including surrogate pairs.
        while pos < len(s):
            if s[pos] == '"':
                try:
                    return json.loads('"' + s[start:pos] + '"')
                except (ValueError,UnicodeError):
                    return ''
            if s[pos] == '\\':
                width = 6 if s[pos:pos+2] == '\\u' else 2
                if pos + width > len(s):
                    break
                if width == 6:
                    try:
                        code = int(s[pos+2:pos+6], 16)
                    except ValueError:
                        return ''
                    if 0xD800 <= code <= 0xDBFF:
                        if pos+12 > len(s) or s[pos+6:pos+8] != '\\u':
                            break
                        width = 12
                pos += width
            else:
                pos += 1
        try:
            value = json.loads('"' + s[start:pos] + '"')
            return value.encode('utf-8').decode('utf-8')
        except (ValueError, UnicodeError):
            return ''
    return ''


def clip(text, units=3200):
    out, count = [], 0
    for char in text:
        count += 2 if ord(char) > 0xFFFF else 1
        if count > units:
            break
        out.append(char)
    return ''.join(out)


class Progress:
    """One delayed placeholder, real partials, <=8 edits; cosmetics fail independently."""
    def __init__(self, app, message):
        self.app, self.m = app, message
        self.cid, self.mid = message['chat']['id'], message['message_id']
        self.draft_id = self.mid or 1
        self.private = message['chat']['type'] == 'private'
        self.thread = message.get('message_thread_id')
        self.task = asyncio.current_task()
        self.pump = None
        self.preview = None
        self.text = self.shown = ''
        self.stage = 'text'
        self.active = self.user_stopped = self.streaming = False
        self.edits = 0
        self.filter = lambda value: value
        self.next_update = 0

    def start(self):
        if self.pump is None:
            self.active = True
            self.app.live_jobs[self.cid, self.draft_id] = self
            self.pump = asyncio.create_task(self.run())

    def delta(self, raw):
        self.streaming = True
        self.text = clip(self.app.safe_text(self.filter(answer_prefix(raw))))

    async def cosmetic(self, method, **payload):
        now = time.monotonic()
        bucket = self.app.live_rates[self.cid]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        global_bucket=self.app.live_rates['global']
        while global_bucket and global_bucket[0]<now-1:
            global_bucket.popleft()
        # Leave room for final answers and other Telegram actions.
        if len(global_bucket)>=20 or method!='sendChatAction' and len(bucket)>=10:
            return None
        global_bucket.append(now)
        if method!='sendChatAction':bucket.append(now)
        try:
            return await self.app.tg(method, _preview=True, **payload)
        except Exception as exc:
            self.next_update = now + max(4, min(60, float(getattr(exc, 'wait', 0))))
            if method == 'sendMessageDraft' and str(getattr(exc, 'code', '')) in {'400', '404'}:
                self.private = False  # Older API: one normal placeholder, no paid retry.
            return None

    async def run(self):
        await asyncio.sleep(.8)  # Fast answers produce no status noise.
        began, typing_at = time.monotonic(), 0
        try:
            while self.active:
                now = time.monotonic()
                scope = {'chat_id': self.cid}
                if self.thread:
                    scope['message_thread_id'] = self.thread
                if now >= self.next_update and now >= typing_at:
                    await self.cosmetic('sendChatAction', **scope, action='upload_photo' if self.stage == 'image' else 'typing')
                    typing_at = now + 4.5
                if now-began >= 1.7 and now >= self.next_update:
                    text = self.text or ('Nexora готовит изображение…' if self.stage == 'image' else 'Nexora думает…')
                    if text != self.shown and self.edits < 8:
                        if self.private:
                            sent = await self.cosmetic('sendMessageDraft', **scope, draft_id=self.draft_id,
                                text=text, can_stop=self.streaming, keep_on_stop=False)
                        elif self.preview:
                            sent = await self.cosmetic('editMessageText', chat_id=self.cid, message_id=self.preview, text=text)
                        else:
                            sent = await self.cosmetic('sendMessage', **scope, text=text, disable_notification=True,
                                reply_parameters={'message_id': self.mid, 'allow_sending_without_reply': True})
                            if isinstance(sent, dict):
                                self.preview = sent.get('message_id')
                        if sent:
                            self.shown = text
                            self.edits += 1
                        self.next_update = max(self.next_update, now+4)
                await asyncio.sleep(.4)
        except asyncio.CancelledError:
            pass

    async def stop_pump(self):
        self.active = False
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)

    async def finish(self, text, keyboard=None):
        await self.stop_pump()
        sent = await self.app.send(self.cid, text, self.mid, keyboard=keyboard, edit=self.preview)
        self.preview = None
        return sent

    async def close(self):
        await self.stop_pump()
        self.app.live_jobs.pop((self.cid, self.draft_id), None)
        if self.preview:
            try:
                await self.app.tg('deleteMessage', _preview=True, chat_id=self.cid, message_id=self.preview)
            except Exception:
                with contextlib.suppress(Exception):
                    await self.app.tg('editMessageText',_preview=True,chat_id=self.cid,message_id=self.preview,
                                      text='Не удалось завершить ответ.')
            self.preview = None


def rate_map():
    from collections import defaultdict
    return defaultdict(deque)
