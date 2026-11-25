import json
import re
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

BRIEFS = Path(__file__).with_name("briefs")
DOMAIN = "https://briefs.dealroom.example/"
URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}]+", re.IGNORECASE)
MAX_LINKS = 3
MAX_SECTIONS = 3
SECTION_FIELDS = {"exclusivity": {"exclusivity"}, "paid-usage": {"paid_usage", "usage_scope"}}
FIELD_PATTERNS = {
    "exclusivity": re.compile(r"(?im)^Exclusivity: ([1-9]\d{0,2}) days?$"),
    "paid_usage": re.compile(r"(?im)^Paid usage: ([1-9]\d{0,2}) days?$"),
    "usage_scope": re.compile(r"(?im)^Paid usage scope: (paid social|paid search|display ads)$"),
}


@lru_cache(maxsize=16)
def brief_document(document_id):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", document_id) or len(document_id) > 80:
        return None
    path = BRIEFS / f"{document_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def linked_brief(url):
    if not url.startswith(DOMAIN):
        return None
    return brief_document(url.removeprefix(DOMAIN))


def links(text):
    return dict.fromkeys(match.group().rstrip(".,;:!?)\"'") for match in URL_PATTERN.finditer(text))


def section_terms(section_id, body):
    values = {
        name: match.group(1).lower()
        for name, pattern in FIELD_PATTERNS.items()
        if name in SECTION_FIELDS.get(section_id, ()) and (match := pattern.search(body))
    }
    result = {}
    if "exclusivity" in values:
        result.update(exclusive=True, exclusive_days=int(values["exclusivity"]))
    if "paid_usage" in values:
        result.update(advertising=True, usage_days=int(values["paid_usage"]))
    if "usage_scope" in values:
        result.update(advertising=True, usage_scope=values["usage_scope"])
    return result


@tool
def read_campaign_brief(url: str, query: str) -> dict:
    """Read a linked document in the Dealroom campaign catalog and return bounded commercial evidence."""
    document = linked_brief(url)
    if not document:
        return {"status": "denied"}
    query_words = set(re.findall(r"[a-z]{3,}", query.lower()[:4000]))
    ranked = []
    for section in document["sections"]:
        terms = section_terms(section["id"], section["body"])
        if not terms:
            continue
        words = set(re.findall(r"[a-z]{3,}", f"{section['title']} {section['body']}".lower()))
        ranked.append((len(words & query_words), section["id"], terms))
    if not ranked:
        return {"status": "empty"}
    selected = sorted(ranked, key=lambda item: (-item[0], item[1]))[:MAX_SECTIONS]
    return {
        "status": "read",
        "document_id": document["id"],
        "title": document["title"],
        "revision": document["revision"],
        "candidate_sections": len(ranked),
        "sections": [{"id": identifier, "terms": terms} for _, identifier, terms in selected],
    }


def collect_evidence(email):
    sources, values, conflicts = [], {}, set()
    selected_links = list(links(email))
    denied = max(0, len(selected_links) - MAX_LINKS)
    for url in selected_links[:MAX_LINKS]:
        result = read_campaign_brief.invoke({"url": url, "query": email})
        if result["status"] != "read":
            denied += 1
            continue
        sections = result["sections"]
        sources.append(
            {
                "document_id": result["document_id"],
                "title": result["title"],
                "revision": result["revision"],
                "section_ids": [item["id"] for item in sections],
                "candidate_sections": result["candidate_sections"],
            }
        )
        for section in sections:
            for field, value in section["terms"].items():
                if field in values and values[field] != value:
                    conflicts.add(field)
                values[field] = value
    return {
        "sources": sources,
        "terms": {field: value for field, value in values.items() if field not in conflicts},
        "denied_links": denied,
        "attempted_links": min(len(selected_links), MAX_LINKS),
        "conflicting_fields": sorted(conflicts),
    }
