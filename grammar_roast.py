"""Detect common grammar errors in user messages and roast the offender in Loki's voice.

Rule-based detection (regex) for high-frequency errors. On match, feed to the
existing LLM handler with a one-shot 'grammar roast' prompt so the roast stays
in Loki's voice.

Env:
  GRAMMAR_ROAST_ENABLED           default 'true'
  GRAMMAR_ROAST_COOLDOWN_SEC      default 1800 (per user, seconds)
  GRAMMAR_ROAST_EXEMPT_USER_IDS   comma-separated Discord user IDs to skip
  GRAMMAR_ROAST_NICKNAMES         comma-separated server nicknames that follow 'you're', not 'your'
"""
import os
import re
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

GRAMMAR_ROAST_ENABLED = os.getenv('GRAMMAR_ROAST_ENABLED', 'true').lower() == 'true'
GRAMMAR_ROAST_COOLDOWN_SEC = int(os.getenv('GRAMMAR_ROAST_COOLDOWN_SEC', '1800'))
GRAMMAR_ROAST_EXEMPT_USER_IDS = {
    uid.strip() for uid in os.getenv('GRAMMAR_ROAST_EXEMPT_USER_IDS', '').split(',') if uid.strip()
}
GRAMMAR_ROAST_NICKNAMES = [
    n.strip() for n in os.getenv('GRAMMAR_ROAST_NICKNAMES', 'bishop').split(',') if n.strip()
]

_last_roast: dict[str, float] = {}

_PREDICATE = r'(welcome|going|coming|doing|being|been|a|an|the|not|so|too|really|always|never|still|probably|gonna|getting|taking|making|trying|thinking|right|wrong|okay|ok|fine|alright|good|bad)'
_POSSESSED = r'(dog|cat|mom|dad|car|house|phone|friend|brother|sister|wife|husband|face|hair|name|job|problem|fault|turn|stuff|thing|things|kid|kids|boss)'

_PATTERNS = [
    ('your->youre',
     re.compile(rf'\byour\s+{_PREDICATE}\b', re.I),
     "'your' is possessive. 'you\u2019re' = 'you are'"),
    ('youre->your',
     re.compile(rf"\byou'?re\s+{_POSSESSED}\b", re.I),
     "'you\u2019re' = 'you are'. Possessive is 'your'"),
    ('their->theyre',
     re.compile(rf'\btheir\s+{_PREDICATE}\b', re.I),
     "'their' is possessive. 'they\u2019re' = 'they are'"),
    ('there->their',
     re.compile(rf'\bthere\s+{_POSSESSED}\b', re.I),
     "'there' = location/expletive. Possessive is 'their'"),
    ('its->its_contraction',
     re.compile(rf'\bits\s+{_PREDICATE}\b', re.I),
     "'its' is possessive. 'it\u2019s' = 'it is'"),
    ('of->have',
     re.compile(r'\b(could|should|would|must|might)\s+of\b', re.I),
     "'X of' is wrong. Use 'X have' or 'X\u2019ve'"),
    ('then->than',
     re.compile(r'\b(better|worse|more|less|greater|smaller|bigger|taller|shorter|faster|slower|cheaper|older|younger|rather|other)\s+then\b', re.I),
     "'than' for comparison. 'then' is for time/sequence"),
    ('loose->lose',
     re.compile(r"\b(gonna|going to|will|i|we|they|you|he|she|don't|dont)\s+loose\b", re.I),
     "'lose' = verb. 'loose' = not tight"),
    ('whos->whose',
     re.compile(rf"\bwho'?s\s+{_POSSESSED}\b", re.I),
     "'who\u2019s' = 'who is'. Possessive is 'whose'"),
]

if GRAMMAR_ROAST_NICKNAMES:
    _nick_alt = '|'.join(re.escape(n) for n in GRAMMAR_ROAST_NICKNAMES)
    _PATTERNS.append((
        'your->youre (nickname)',
        re.compile(rf'\byour\s+({_nick_alt})\b', re.I),
        "'your' is possessive. You meant 'you\u2019re' — they're a person, not a belonging",
    ))

_DISTRESS_RE = re.compile(
    r"\b(fuck you|fuck off|stfu|shut the fuck up|hate (this|you|my life)|i'?m done|"
    r"leave me alone|seriously stop|kill myself|kms|i want to die|depressed|suicid)\b",
    re.I,
)

_URL_RE = re.compile(r'https?://\S+', re.I)
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```')
_INLINE_CODE_RE = re.compile(r'`[^`]+`')


def _strip_noise(text: str) -> str:
    text = _CODE_BLOCK_RE.sub(' ', text)
    text = _INLINE_CODE_RE.sub(' ', text)
    text = _URL_RE.sub(' ', text)
    return text


def _detect(text: str) -> Optional[tuple[str, str, str]]:
    clean = _strip_noise(text)
    for label, pattern, hint in _PATTERNS:
        m = pattern.search(clean)
        if m:
            return label, m.group(0), hint
    return None


async def maybe_grammar_roast(message, llm_handler, system_prompt: str) -> bool:
    """Detect a grammar error and post a Loki-voice roast reply. Returns True if sent."""
    if not GRAMMAR_ROAST_ENABLED:
        return False
    if getattr(message.author, 'bot', False):
        return False
    content = message.content or ''
    if len(content) < 8:
        return False
    if content.startswith(('!', '/', '.')):
        return False
    if str(message.author.id) in GRAMMAR_ROAST_EXEMPT_USER_IDS:
        return False
    if _DISTRESS_RE.search(content):
        return False

    result = _detect(content)
    if not result:
        return False

    uid = str(message.author.id)
    now = time.time()
    if now - _last_roast.get(uid, 0) < GRAMMAR_ROAST_COOLDOWN_SEC:
        return False

    label, matched, hint = result
    _last_roast[uid] = now
    log.info(f"Grammar roast: {message.author.display_name} — {label} ('{matched}')")

    prompt = [
        {'role': 'system', 'content': (
            system_prompt + '\n\n'
            'The user just made a grammar error. Roast them SPECIFICALLY for this mistake in 1-2 '
            'sharp sentences in your Loki voice. Include the correction inline so they actually '
            'learn. No greetings, no "but seriously", no compliment sandwich. End on the knife.'
        )},
        {'role': 'user', 'content': (
            f'{message.author.display_name} just wrote: "{content[:400]}"\n\n'
            f'Error type: {label}. Offending phrase: "{matched}". Rule: {hint}.\n\n'
            'Roast them and correct them.'
        )},
    ]
    try:
        reply = await llm_handler.chat(prompt)
        if reply and reply.strip():
            await message.reply(reply[:1900], mention_author=False)
            return True
    except Exception as e:
        log.error(f'Grammar roast send failed: {e}')
    return False
