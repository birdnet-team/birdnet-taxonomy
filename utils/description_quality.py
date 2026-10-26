"""Shared helpers for description quality checks."""

from __future__ import annotations

import re

from collectors._common import same_epithet_stem


def word_count(text: str) -> int:
    """Count prose units for quality checks, including CJK text without spaces."""
    text = text or ""
    words = re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)
    cjk_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
    return len(words) + len(cjk_chars)


# Opening phrases of Wikipedia disambiguation pages ("Ruff may refer to:"),
# which iNat sometimes links instead of the species article.
_DISAMBIGUATION_RE = re.compile(
    r"^[^\n]{0,160}?\b("
    r"may (?:also )?refer to|can refer to|"           # en
    r"steht für|bezeichnet|"                          # de
    r"peut (?:désigner|faire référence)|"             # fr
    r"puede (?:referirse|hacer referencia)|"          # es
    r"pode (?:referir-se|designar)|"                  # pt
    r"può (?:riferirsi|indicare)|"                    # it
    r"kan (?:verwijzen naar|syfta på|henvise til|vise til)|"  # nl/sv/da/no
    r"może (?:oznaczać|odnosić się)|"                 # pl
    r"může (?:být|označovat)|"                        # cs
    r"может (?:означать|относиться)"                  # ru
    r")\b[^\n]{0,40}:\s*(\n|$)",
    re.IGNORECASE,
)


def is_disambiguation_text(text: str) -> bool:
    """Return True for Wikipedia disambiguation-page text (a list, not a description)."""
    return bool(_DISAMBIGUATION_RE.search((text or "").strip()))


def normalize_space(text: str) -> str:
    """Collapse whitespace in text."""
    return re.sub(r"\s+", " ", (text or "").strip())


def contains_species_identity(text: str, names: list[str] | tuple[str, ...] | set[str]) -> bool:
    """Return True when text contains one of the accepted scientific names."""
    haystack = normalize_space(text).lower()
    if not haystack:
        return False
    for name in names:
        clean = normalize_space(str(name)).lower()
        if clean and re.search(rf"\b{re.escape(clean)}\b", haystack):
            return True
    return False


# Wikipedia plain-text extracts mark sections as "== Title ==" (any level).
_HEADING_RE = re.compile(r"^[ \t]*(={2,})[ \t]*(.+?)[ \t]*\1[ \t]*$", re.MULTILINE)

# Sections that end the useful part of an article.
_END_SECTIONS = re.compile(
    r"^(references|notes|external links|further reading|see also|bibliography|"
    r"sources|gallery|citations|"
    r"literatur|weblinks|einzelnachweise|anmerkungen|siehe auch|"
    r"références|notes et références|liens externes|bibliographie|voir aussi|"
    r"referencias|enlaces externos|bibliografía|véase también|"
    r"referências|ligações externas|bibliografia|ver também|"
    r"note|collegamenti esterni|voci correlate|"
    r"referenties|externe links|zie ook|"
    r"przypisy|linki zewnętrzne|zobacz też|"
    r"reference|externí odkazy|literatura|související články|"
    r"примечания|литература|ссылки|см\. также|"
    r"referenser|externa länkar|se även|"
    r"referencer|eksterne henvisninger|referanser|eksterne lenker)\b",
    re.IGNORECASE,
)

# Sections about names and classification rather than the animal itself.
_SKIP_SECTIONS = re.compile(
    r"^(taxonomy|systematics|classification|etymology|nomenclature|names?|"
    r"subspecies|evolution|phylogeny|fossil record|"
    r"systematik|taxonomie|etymologie|namensgebung|unterarten|"
    r"systématique|taxinomie|taxonomie|étymologie|dénomination|sous-espèces|"
    r"taxonomía|sistemática|etimología|subespecies|"
    r"taxonomia|sistemática|etimologia|subespécies|"
    r"tassonomia|sistematica|etimologia|sottospecie|"
    r"taxonomie|systematiek|etymologie|ondersoorten|"
    r"systematyka|taksonomia|etymologia|podgatunki|"
    r"systematika|taxonomie|etymologie|poddruhy|"
    r"систематика|таксономия|этимология|подвиды)\b",
    re.IGNORECASE,
)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split a plain-text extract into (heading, body) pairs; the lead has heading ""."""
    parts = _HEADING_RE.split(text)
    sections = [("", parts[0])]
    for i in range(1, len(parts) - 2, 3):
        sections.append((parts[i + 1].strip(), parts[i + 2]))
    return sections


def _paragraphs(text: str) -> list[str]:
    return [normalize_space(p) for p in re.split(r"\n{2,}", text) if normalize_space(p)]


def _cap_words(paragraphs: list[str], max_words: int) -> list[str]:
    """Keep whole paragraphs up to max_words; cut the overflowing one at a sentence."""
    out: list[str] = []
    total = 0
    for para in paragraphs:
        words = word_count(para)
        if total + words <= max_words or not out:
            out.append(para)
            total += words
            continue
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            if total + word_count(sentence) > max_words:
                break
            kept.append(sentence)
            total += word_count(sentence)
        if kept:
            out.append(" ".join(kept))
        break
    return out


_CITATION_RE = re.compile(r"\s*\[(?:\d+|[a-z]|citation needed|clarification needed|note \d+)\]", re.IGNORECASE)
_MARKUP_RE = re.compile(r"\{\{|\}\}|\[\[|\]\]|<ref|</ref|<br|\|\s*\w+\s*=")


def _clean_paragraph(paragraph: str, seen: set[str]) -> str:
    """Drop citation marks, sentences with leftover wiki markup, and repeats."""
    paragraph = _CITATION_RE.sub("", paragraph.replace("\u200b", ""))
    # Headings flattened into running text ("... montaña. == Referencias ==")
    for match in list(re.finditer(r"\s*(={2,})\s*([^=\n]+?)\s*\1", paragraph)):
        if _END_SECTIONS.match(match.group(2).strip()):
            paragraph = paragraph[:match.start()]
            break
    paragraph = re.sub(r"\s*(={2,})\s*[^=\n]+?\s*\1\s*", " ", paragraph)
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?。])\s+|\n", paragraph):
        key = sentence.strip().lower()
        if (not key or _MARKUP_RE.search(sentence) or re.match(r"[,;:)\]}|]", key)
                or (len(key) > 30 and key in seen)):
            continue
        seen.add(key)
        kept.append(sentence.strip())
    return normalize_space(" ".join(kept))


def clean_description_text(text: str) -> str:
    """Clean stored descriptions: drop section headings, reference-type
    tails, citation marks, sentences with leftover wiki markup, and repeated
    sentences."""
    if not text:
        return text
    kept: list[str] = []
    seen: set[str] = set()
    for heading, body in _split_sections(text.replace("\r\n", "\n")):
        if heading and _END_SECTIONS.match(heading):
            break
        for paragraph in re.split(r"\n{2,}", body):
            cleaned = _clean_paragraph(paragraph, seen)
            if cleaned:
                kept.append(cleaned)
    return "\n\n".join(kept).strip()


def select_description_text(full_text: str, min_words: int, target_words: int,
                            extra_sections: int = 2,
                            min_paragraphs: int = 1,
                            max_words: int = 0) -> str:
    """Select a concise early-article description from a full Wikipedia extract.

    Takes the lead, then up to extra_sections further sections about the
    animal (skipping taxonomy/etymology, stopping at references), until
    target_words is reached; never emits section headings and caps the
    result at max_words (default: twice target_words).
    """
    text = (full_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    goal = max(min_words, target_words)
    min_chunks = max(1, int(min_paragraphs or 1))
    selected: list[str] = []
    used_sections = 0
    for heading, body in _split_sections(text):
        if heading:
            if _END_SECTIONS.match(heading):
                break
            if _SKIP_SECTIONS.match(heading):
                continue
            if used_sections >= int(extra_sections or 0):
                break
            used_sections += 1
        for para in _paragraphs(body):
            selected.append(para)
            if len(selected) >= min_chunks and word_count(" ".join(selected)) >= goal:
                break
        if len(selected) >= min_chunks and word_count(" ".join(selected)) >= goal:
            break
    selected = _cap_words(selected, int(max_words or 0) or target_words * 2)
    return "\n\n".join(selected).strip()


_TEXT_BINOMIAL_RE = re.compile(
    r'"?\b([A-Z][a-z]+)"?\s+(?:kl\.\s+|\([A-Z][a-z]+\)\s+)?([a-z][a-z-]{2,})\b')


def describes_species(text: str, title: str, names: list[str],
                       common_name: str = "", other_species: set[str] | None = None) -> bool:
    """True if a Wikipedia text is about this species rather than a sister
    species, genus, or clade (iNat sometimes links those articles).

    The text must name the species: its scientific name or an alias (also as
    a trinomial or with a subgenus), or a binomial that differs only by genus
    or epithet ending (unrecorded genus transfer) and is not another species
    in the taxonomy. Articles titled with the species' English name and the
    articles of monotypic genera pass too.
    """
    if contains_species_identity(f"{title}\n{text}", names):
        return True
    if common_name and title.strip().lower() == common_name.strip().lower():
        return True
    for name in names:
        genus, epithet = name.split()[:2]
        if re.search(rf"\b{re.escape(genus)}\s+(?:\(?[A-Za-z]+\)?\s+)?{re.escape(epithet)}\b",
                     text, re.IGNORECASE):
            return True
    epithets = [n.split()[1] for n in names]
    for genus, epithet in _TEXT_BINOMIAL_RE.findall(text[:600]):
        binomial = f"{genus} {epithet}"
        if binomial in (other_species or set()):
            continue
        if any(same_epithet_stem(epithet, e) for e in epithets):
            return True
    genera = {n.split()[0] for n in names}
    return title in genera and bool(re.search(
        r"\bmonotypic\b|\b(?:only|sole|single|one)\b[^.]{0,40}\bspecies\b", text, re.IGNORECASE))



def names_other_species(text: str, names: list[str], other_species: set[str]) -> bool:
    """True if a text's opening names another species (in parentheses, as
    Wikipedia leads do) and never this one — a sister-species article.

    Texts without a binomial are not judged (many non-English articles don't
    repeat the scientific name).
    """
    match = re.search(r"\(([A-Z][a-z]+ [a-z][a-z-]+)[,)]", (text or "")[:300])
    if not match or match.group(1) not in other_species or match.group(1) in names:
        return False
    return not describes_species(text, "", names, other_species=other_species)
