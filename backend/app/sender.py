import re
import unicodedata
from dataclasses import dataclass
from email import policy
from email.parser import HeaderParser
from typing import Literal

Evidence = Literal["mailbox", "signature", "ambiguous", "unverified"]
HEADER = re.compile(r"(?i)^(from|to|subject|reply-to|date|cc):")
QUOTE = re.compile(
    r"(?im)^\s*(?:On .{1,240} wrote:|[- ]*Original Message[- ]*|[- ]*Forwarded message[- ]*)\s*$"
)
SIGNOFF = re.compile(
    r"(?i)^(?:best|thanks(?: again)?|thank you|cheers|kind regards|regards|all the best)[,!]?(?:[ \t]+(.*))?$"
)
GENERIC = {"team", "support", "marketing", "partnerships", "brand", "sales", "hello", "info", "admin"}


@dataclass(frozen=True)
class Sender:
    first_name: str | None = None
    evidence: Evidence = "unverified"


def split_email(message):
    header, separator, body = message.replace("\r\n", "\n").partition("\n\n")
    if not separator or not HEADER.match(header):
        return "", message
    return header, body


def current_body(message):
    _, body = split_email(message)
    body = QUOTE.split(body, maxsplit=1)[0]
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">")).strip()


def name_parts(value):
    words = value.split()
    if not 1 <= len(words) <= 4:
        return ()
    if any(
        not 2 <= len(word) <= 28 or not all(part.isalpha() for part in re.split("[-'’]", word))
        for word in words
    ):
        return ()
    return tuple(words) if normalize(words[0]) not in GENERIC else ()


def normalize(text):
    return "".join(ch for ch in unicodedata.normalize("NFKD", text.casefold()) if ch.isalnum())


def signature(body):
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for index in range(len(lines) - 1, max(-1, len(lines) - 9), -1):
        match = SIGNOFF.fullmatch(lines[index])
        if match:
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            return name_parts(match[1] or next_line)
    return ()


def mailbox_matches(names, username):
    tokens = {normalize(part) for part in re.split(r"[._-]", username.split("+", 1)[0])}
    first, last = normalize(names[0]), normalize(names[-1])
    if first in tokens:
        return True
    if len(names) == 1:
        return False
    return bool(tokens & {first + last, first + last[:1], first[:1] + last})


def identify_sender(message):
    headers, _ = split_email(message)
    if not headers:
        return Sender()
    try:
        parsed = HeaderParser(policy=policy.default).parsestr(headers)
        senders = parsed.get_all("From", [])
        if len(senders) > 1:
            return Sender(evidence="ambiguous")
        if not senders or senders[0].defects or len(senders[0].addresses) != 1:
            return Sender()
        address = senders[0].addresses[0]
    except (ValueError, IndexError):
        return Sender()
    if not address.username or not address.domain:
        return Sender()
    stated, signed = name_parts(address.display_name), signature(current_body(message))
    if stated and signed:
        first_conflict = normalize(stated[0]) != normalize(signed[0])
        last_conflict = (
            len(stated) > 1 and len(signed) > 1 and normalize(stated[-1]) != normalize(signed[-1])
        )
        if first_conflict or last_conflict:
            return Sender(evidence="ambiguous")
    if stated and mailbox_matches(stated, address.username):
        first = stated[0]
        return Sender(first[0].upper() + first[1:], "mailbox")
    if signed and (stated or mailbox_matches(signed, address.username)):
        first = signed[0]
        return Sender(first[0].upper() + first[1:], "signature")
    return Sender()
