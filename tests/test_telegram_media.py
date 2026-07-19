"""
Mocked tests for Telegram photo + voice support (telegram_interface.py).

No network, no real Telegram, no real vision/Whisper calls: the Bot API
(_api), the file-download session, and the injected describe_image /
transcribe_audio pipelines are all fakes. Verifies the media flows reuse
the shared Discord pipelines, honor limits, clean temp files, dedupe
re-delivered updates, enforce owner auth, and never leak the bot token or
private content into logs.

Run:  venv/bin/python -m unittest tests.test_telegram_media -v
"""

import logging
import os
import unittest

import telegram_interface
from telegram_interface import TelegramInterface

FAKE_TOKEN = "1234567890:FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAK"
OWNER_ID = 4242
STRANGER_ID = 6666

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 200
OGG = b"OggS" + b"o" * 200


class FakeResp:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Serves file-download GETs; records URLs so tests can assert on them."""

    def __init__(self, body=JPEG, status=200, raise_exc=None):
        self.body = body
        self.status = status
        self.raise_exc = raise_exc
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if self.raise_exc:
            raise self.raise_exc
        return FakeResp(self.status, self.body)


class FakeLLM:
    def __init__(self, reply="Handled it, Boss."):
        self.reply = reply
        self.calls = []

    async def chat_with_tools(self, messages, ctx):
        self.calls.append(messages)
        return self.reply


def photo_msg(caption=None, message_id=1, user_id=OWNER_ID, sizes=None):
    msg = {"chat": {"id": 100}, "message_id": message_id,
           "from": {"id": user_id, "first_name": "Boss"},
           "photo": sizes if sizes is not None else [
               {"file_id": "small", "width": 90, "height": 90, "file_size": 900},
               {"file_id": "big", "width": 1280, "height": 960, "file_size": 90_000},
           ]}
    if caption:
        msg["caption"] = caption
    return msg


def voice_msg(message_id=2, user_id=OWNER_ID, duration=5,
              file_size=9_000, mime="audio/ogg"):
    return {"chat": {"id": 100}, "message_id": message_id,
            "from": {"id": user_id, "first_name": "Boss"},
            "voice": {"file_id": "v1", "duration": duration,
                      "file_size": file_size, "mime_type": mime}}


class MediaTestBase(unittest.IsolatedAsyncioTestCase):
    def make_iface(self, body=JPEG, dl_status=200, dl_exc=None,
                   vision_result="a cat on a couch",
                   transcript="create a list called groceries",
                   vision_exc=None, transcribe_exc=None):
        self.session = FakeSession(body=body, status=dl_status, raise_exc=dl_exc)
        self.llm = FakeLLM()
        self.vision_calls = []
        self.transcribe_calls = []
        self.api_calls = []
        self.spooled = []

        async def describe_image(data, mime):
            self.vision_calls.append((data, mime))
            if vision_exc:
                raise vision_exc
            return vision_result

        async def transcribe_audio(data, filename):
            self.transcribe_calls.append((data, filename))
            if transcribe_exc:
                raise transcribe_exc
            return transcript

        async def session_factory():
            return self.session

        iface = TelegramInterface(
            llm=self.llm,
            tool_ctx_factory=lambda uid, name, cid: {"uid": uid},
            session_factory=session_factory,
            describe_image=describe_image,
            transcribe_audio=transcribe_audio,
        )
        iface.token = FAKE_TOKEN
        iface.owner_id = OWNER_ID

        async def fake_api(method, token=None, timeout=15, **params):
            self.api_calls.append((method, params))
            if method == "getFile":
                return {"file_path": "media/file_1.dat",
                        "file_size": len(self.session.body)}
            return {"message_id": 999}

        iface._api = fake_api

        def spool_spy(data, suffix):
            path = TelegramInterface._spool_to_temp(data, suffix)
            self.spooled.append(path)
            return path

        iface._spool_to_temp = spool_spy
        return iface

    def sent_texts(self):
        return [p.get("text", "") for m, p in self.api_calls
                if m == "sendMessage"]


class PhotoTests(MediaTestBase):
    async def test_photo_reaches_vision_handler(self):
        iface = self.make_iface()
        await iface._handle(photo_msg(caption="what is this?"))
        self.assertEqual(len(self.vision_calls), 1)
        self.assertEqual(self.vision_calls[0][0], JPEG)
        self.assertEqual(self.vision_calls[0][1], "image/jpeg")

    async def test_largest_suitable_variant_selected(self):
        huge = telegram_interface.IMAGE_MAX_BYTES + 1
        sizes = [
            {"file_id": "small", "width": 90, "height": 90, "file_size": 900},
            {"file_id": "mid", "width": 800, "height": 600, "file_size": 50_000},
            {"file_id": "toobig", "width": 4000, "height": 3000, "file_size": huge},
        ]
        iface = self.make_iface()
        await iface._handle(photo_msg(sizes=sizes))
        getfile = [p for m, p in self.api_calls if m == "getFile"]
        self.assertEqual(getfile, [{"file_id": "mid"}])

    async def test_caption_is_included(self):
        iface = self.make_iface()
        await iface._handle(photo_msg(caption="is this our cat?"))
        user_msg = self.llm.calls[0][-1]["content"]
        self.assertIn("is this our cat?", user_msg)
        self.assertIn("a cat on a couch", user_msg)

    async def test_no_caption_uses_neutral_prompt(self):
        iface = self.make_iface()
        await iface._handle(photo_msg())
        user_msg = self.llm.calls[0][-1]["content"]
        self.assertIn("image", user_msg.lower())
        self.assertIn("a cat on a couch", user_msg)

    async def test_unsupported_format_rejected(self):
        iface = self.make_iface()
        msg = {"chat": {"id": 100}, "message_id": 10,
               "from": {"id": OWNER_ID, "first_name": "Boss"},
               "document": {"file_id": "d1", "mime_type": "image/tiff",
                            "file_size": 5000, "file_name": "scan.tiff"}}
        await iface._handle(msg)
        self.assertEqual(self.vision_calls, [])
        self.assertTrue(any("format" in t for t in self.sent_texts()))

    async def test_not_really_an_image_rejected(self):
        iface = self.make_iface(body=b"not an image at all" * 10)
        await iface._handle(photo_msg())
        self.assertEqual(self.vision_calls, [])
        self.assertTrue(any("doesn't look like an image" in t
                            for t in self.sent_texts()))

    async def test_oversized_image_rejected(self):
        huge = telegram_interface.IMAGE_MAX_BYTES + 1
        sizes = [{"file_id": "only", "width": 4000, "height": 3000,
                  "file_size": huge}]
        iface = self.make_iface()
        await iface._handle(photo_msg(sizes=sizes))
        self.assertEqual(self.vision_calls, [])
        self.assertEqual(self.session.urls, [])
        self.assertTrue(any("too large" in t for t in self.sent_texts()))

    async def test_download_failure_handled(self):
        iface = self.make_iface(dl_status=404)
        await iface._handle(photo_msg())
        self.assertEqual(self.vision_calls, [])
        self.assertTrue(any("couldn't download" in t for t in self.sent_texts()))

    async def test_temp_file_removed_after_success(self):
        iface = self.make_iface()
        await iface._handle(photo_msg())
        self.assertEqual(len(self.spooled), 1)
        self.assertFalse(os.path.exists(self.spooled[0]))

    async def test_temp_file_removed_after_vision_failure(self):
        iface = self.make_iface(vision_exc=RuntimeError("vision down"))
        await iface._safe_handle(photo_msg())
        self.assertEqual(len(self.spooled), 1)
        self.assertFalse(os.path.exists(self.spooled[0]))

    async def test_unauthorized_user_denied(self):
        iface = self.make_iface()
        await iface._handle(photo_msg(user_id=STRANGER_ID))
        self.assertEqual(self.vision_calls, [])
        self.assertEqual(self.llm.calls, [])
        self.assertTrue(any("private line" in t for t in self.sent_texts()))

    async def test_token_never_logged_on_success_or_failure(self):
        with self.assertLogs(level=logging.DEBUG) as cap:
            iface = self.make_iface()
            await iface._handle(photo_msg(message_id=50))
            iface2 = self.make_iface(
                dl_exc=Exception(
                    f"boom https://api.telegram.org/file/bot{FAKE_TOKEN}/x"))
            await iface2._handle(photo_msg(message_id=51))
        joined = "\n".join(r.getMessage() for r in cap.records)
        self.assertNotIn(FAKE_TOKEN, joined)


class VoiceTests(MediaTestBase):
    async def test_voice_reaches_transcription(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(voice_msg())
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(self.transcribe_calls[0][0], OGG)

    async def test_ogg_opus_accepted(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(voice_msg(mime="audio/ogg"))
        self.assertEqual(self.transcribe_calls[0][1], "voice.ogg")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_transcript_reaches_chat_with_tools(self):
        iface = self.make_iface(body=OGG,
                                transcript="create a list called groceries")
        await iface._handle(voice_msg())
        user_msg = self.llm.calls[0][-1]["content"]
        self.assertEqual(user_msg, "create a list called groceries")

    async def test_loki_responds_to_content(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(voice_msg())
        self.assertIn("Handled it, Boss.", self.sent_texts())

    async def test_transcription_failure_is_honest(self):
        iface = self.make_iface(body=OGG, transcript="")
        await iface._handle(voice_msg())
        self.assertEqual(self.llm.calls, [])
        self.assertTrue(any("couldn't make out" in t for t in self.sent_texts()))

    async def test_over_duration_audio_rejected(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(
            voice_msg(duration=telegram_interface.AUDIO_MAX_SECONDS + 1))
        self.assertEqual(self.transcribe_calls, [])
        self.assertTrue(any("too long" in t for t in self.sent_texts()))

    async def test_oversized_audio_rejected(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(
            voice_msg(file_size=telegram_interface.AUDIO_MAX_BYTES + 1))
        self.assertEqual(self.transcribe_calls, [])
        self.assertTrue(any("too large" in t for t in self.sent_texts()))

    async def test_temp_files_always_removed(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(voice_msg(message_id=60))
        paths = list(self.spooled)
        iface2 = self.make_iface(body=OGG,
                                 transcribe_exc=RuntimeError("whisper down"))
        await iface2._safe_handle(voice_msg(message_id=61))
        paths += self.spooled
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertFalse(os.path.exists(path))

    async def test_duplicate_update_not_processed_twice(self):
        iface = self.make_iface(body=OGG)
        msg = voice_msg(message_id=70)
        await iface._handle(msg)
        await iface._handle(dict(msg))
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_unauthorized_user_denied(self):
        iface = self.make_iface(body=OGG)
        await iface._handle(voice_msg(user_id=STRANGER_ID))
        self.assertEqual(self.transcribe_calls, [])
        self.assertEqual(self.llm.calls, [])

    async def test_audio_and_transcript_never_logged(self):
        secret = "the gate code is nine nine one two"
        with self.assertLogs(level=logging.DEBUG) as cap:
            iface = self.make_iface(body=OGG, transcript=secret)
            await iface._handle(voice_msg(message_id=80))
        joined = "\n".join(r.getMessage() for r in cap.records)
        self.assertNotIn(secret, joined)
        self.assertNotIn("OggS", joined)
        self.assertNotIn(FAKE_TOKEN, joined)


if __name__ == "__main__":
    unittest.main()
