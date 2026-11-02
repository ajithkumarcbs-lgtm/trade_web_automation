"""Voice notes -> text with OpenAI speech-to-text (or Groq's free Whisper), so tips given by voice are read like
typed ones."""
from __future__ import annotations

import logging

import openai

from .signal_parser import GROQ_URL, ai_client

log = logging.getLogger(__name__)

FALLBACK_MODEL = "whisper-1"
GROQ_MODEL = "whisper-large-v3-turbo"  # Groq's free speech-to-text
# Words the tipsters use, so the transcription spells them the way the channels do.
PROMPT = ("Tamil and English mixed talk (Thanglish) by a cricket and tennis betting tipster: back, lay, odds, "
          "limit, unit, bookset, book set, losscut, loss cut, exit, jackpot, session, lambi, over, wicket, "
          "favourite, profit, loss, entry, cashout.")
SPEECH_KINDS = ("voice", "video_note", "audio")
MAX_BYTES = 25 * 1024 * 1024  # OpenAI's limit for one file


class Transcriber:
    def __init__(self, api_key: str, model: str = "gpt-4o-transcribe", groq: bool = False):
        """groq: api_key is a Groq key, and its Whisper is used (model ignored)."""
        self._ai = ai_client(api_key, GROQ_URL if groq else None)
        self._model = GROQ_MODEL if groq else model

    async def __call__(self, data: bytes, filename: str) -> str:
        """Text of the recording; raises openai.APIError when OpenAI can't be reached or refuses."""
        if len(data) > MAX_BYTES:
            raise ValueError(f"recording too big to transcribe ({len(data) // 1_048_576} MB)")
        try:
            result = await self._ai.audio.transcriptions.create(model=self._model, file=(filename, data),
                                                                prompt=PROMPT)
        except openai.NotFoundError:  # this key can't use the model: the older one always works
            log.warning("%s not available, using %s", self._model, FALLBACK_MODEL)
            self._model = FALLBACK_MODEL
            result = await self._ai.audio.transcriptions.create(model=self._model, file=(filename, data),
                                                                prompt=PROMPT)
        return result.text.strip()
