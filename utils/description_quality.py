"""Shared helpers for description quality checks."""

from __future__ import annotations

import html
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
    r"referencer|eksterne henvisninger|referanser|eksterne lenker|"
    r"original publication|publication originale|publicación original|"
    r"pubblicazione originale|publicação original|originele publicatie|"
    r"lähteet|kirjallisuutta|aiheesta muualla|kaynakça|kaynaklar|dış bağlantılar|"
    r"ayrıca bakınız|مراجع|مصادر|وصلات خارجية|انظر أيضا|脚注|参考文献|関連項目|"
    r"外部リンク|外部链接|参见|注释|각주|참고 문헌|외부 링크|marejeo|viungo vya nje)\b",
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
# Inline archive-link notes in running text ("Archived 2006-01-10 at the
# Wayback Machine."); in link lists (tr, da) they end the paragraph instead.
_ARCHIVED_RE = re.compile(
    r"\s*(?:Archived|Archivado el|Archivováno|Archiviato il)\b[^\n]{0,40}?"
    r"(?:at the|en|na|in)\s+Wayback Machine\.?(?=\s+(\S)|$)")
_ARCHIVED_INLINE_RE = re.compile(
    r"\s*（(?:页面存档备份|原始内容存档)[^）]*）|"
    r"\s*نسخة محفوظة \d{1,2} \S+ \d{4} على موقع واي باك مشين")
_URL_PAREN_RE = re.compile(r"\s*[(（][^()（）]*(?:https?://|www\.)[^()（）]*[)）]")

# Reference-list text that survives section selection ("Günther, 1863 :
# On new species ... Annals and Magazine of Natural History, vol. 11").
_BIB_MARKER_RE = re.compile(
    r"\bISBN\b|\bdoi:|\bdoi\.org\b|Wayback Machine|\bViitattu \d|\bIn: [A-Z]|"
    r"^\s*\([a-z]{2,3}(?: \+ [a-z]{2,3})*\) |"  # fr source-language tag: "(en) Gould, « ... »"
    r"\bRetrieved (?:\d{1,2} )?[A-Z][a-z]+ \d|\b(?:[Cc]onsultado el|consulté le|abgerufen am) \d|"
    r"tarihinde (?:alındı|erişildi)|kaynağından arşivlendi|Downloaded from|https?://|\bwww\.|"
    r"\(englanniksi\)|\(Arkistoitu|\bLaji\.fi\b|\bARKive\b|Species Account|"
    r"Internet Bird Collection(?:\s*[.》)]|\s+(?:Resim|Wikimedia|Lynx))|"
    r"\b[Vv]ideos? (?:on|by|from|of)\b[^.]{0,60}Internet Bird Collection|"
    r"Wikimedia Commons'ta|Vikitür'de|\(PDF\b")
# A sentence that is only an author list ("Helb, H.-W.; Bergmann, H.-H.").
_BIB_AUTHOR_LIST_RE = re.compile(
    r"(?:[A-ZÀ-Þ][\w'’-]+,\s+(?:[A-Z]\.[\s-]*)+;\s*)+[A-ZÀ-Þ][\w'’-]+,\s+(?:[A-Z]\.[\s-]*)+")
_BIB_AUTHORS_RE = re.compile(
    r"^(?:文献\s*)?(?:(?:[A-ZÀ-ÖØ-Þ][\w'’-]*\.?|&|and|et|y|e|und|de|da|van|von|der|den|du|le|la|al\.|Jr\.?)"
    r"[\s,;]*){1,15}\(?(?:1[789]|20)\d{2}[a-z]?\)?(?:\s*\"\d{4}\")?\s*[:.)]")
_BIB_VENUE_RE = re.compile(
    r"\bvol\.|\bs?[ps]p?\.\s*\d|\b\d+\s*\(\d+\)\s*:\s*\d|\bJournal\b|\bProceedings\b|\bBulletin\b|\bAnnal[se]s?\b|Zootaxa|"
    r"Copeia|Novitates|Herpetologica|Zeitschrift|\bRevue\b|\bMus(?:eum|ée|eo|eu)\b|University Press|"
    r"Verlag|texte intégral|Full text|Handbook|\bSoc(?:iety|iété)?\b|\bActa\b|\bZool", re.IGNORECASE)


# A subspecies with its author ("C. a. aequabilis Ripley, 1945"); a paragraph
# with several is a subspecies list.
_LATIN_END = r"(?:us|a|um|i|ae|is|ii|ensis|oides|or|er|ix|ax|ex|os|es|on)"
_TRINOMIAL_AUTHOR_RE = re.compile(
    rf"\b[A-Z][a-z]*\.? (?:[a-z]\.|[a-z]+{_LATIN_END}) [a-z]{{2,}}{_LATIN_END},? "
    r"\(?(?:[A-ZÀ-Þ][\w'’.-]+(?: (?:&|and|et|y|und) |, ?| )?)+,? ?(?:1[789]|20)\d{2}\)?")
# An abbreviated subspecies introducing its range or traits ("N. l.
# yucatanicus - Central America", "T. a. distans: males are darker").
_TRINOMIAL_ITEM_RE = re.compile(r"\b[A-Z]\. [a-z]+\.? [a-z]{3,}\b\s*(?:[–—:-]|\()")
# A sentence of bare scientific names (a synonym list).
_NAME_LIST_RE = re.compile(r"(?:[A-Z][a-z]+ [a-z]{3,}[,;]?\s*){2,}")
_CATEGORY_RE = re.compile(r"\b(?:[KkL]ategori[ae]?|Category|Catégorie|Categoría|Categoria):\S")


def _drop_list_intro(paragraph: str) -> str:
    """Drop a closing sentence that introduces a list ("Three subspecies are
    recognised:") whose items are gone."""
    while paragraph.rstrip().endswith((":", "：")):
        parts = re.split(r"(?<=[.!?。])\s+", paragraph.rstrip())
        paragraph = " ".join(parts[:-1])
    return paragraph


def _starts_citation(sentence: str, rest: str) -> bool:
    """True if a sentence begins reference-list text (rest: it and what follows)."""
    return bool(_BIB_MARKER_RE.search(sentence) or _CATEGORY_RE.search(sentence)
                or _BIB_AUTHOR_LIST_RE.fullmatch(sentence.strip())
                or (_BIB_AUTHORS_RE.match(rest) and _BIB_VENUE_RE.search(rest)))


def _clean_paragraph(paragraph: str, seen: set[str]) -> str:
    """Drop citation marks, sentences with leftover wiki markup, and repeats."""
    paragraph = _CITATION_RE.sub("", html.unescape(paragraph.replace("\u200b", "")))
    paragraph = re.sub(r"&[a-z]*dash;", "–", paragraph)
    # Headings flattened into running text ("... montaña. == Referencias ==")
    for match in list(re.finditer(r"\s*(={2,})\s*([^=\n]+?)\s*\1", paragraph)):
        if _END_SECTIONS.match(match.group(2).strip()):
            paragraph = paragraph[:match.start()]
            break
    paragraph = re.sub(r"\s*(={2,})\s*[^=\n]+?\s*\1\s*", " ", paragraph)
    paragraph = _ARCHIVED_RE.sub(
        lambda m: "" if m.group(1) and m.group(1).islower() else ".", paragraph)
    paragraph = _URL_PAREN_RE.sub("", _ARCHIVED_INLINE_RE.sub("", paragraph))
    kept: list[str] = []
    sentences = re.split(r"(?<=[.!?。])\s+|\n", paragraph)
    for i, sentence in enumerate(sentences):
        if _starts_citation(sentence, " ".join(sentences[i:])):
            break
        key = sentence.strip().lower()
        if (not key or _MARKUP_RE.search(sentence) or re.match(r"[,;:)\]{}|]", key)
                or _NAME_LIST_RE.fullmatch(sentence.strip())
                or (len(key) > 30 and key in seen)):
            continue
        seen.add(key)
        kept.append(sentence.strip())
    return normalize_space(" ".join(kept))


def clean_description_text(text: str) -> str:
    """Clean stored descriptions: drop section headings, reference-type
    tails and reference-list text, subspecies and synonym lists, citation
    marks, sentences with leftover wiki markup, and repeated sentences."""
    if not text:
        return text
    kept: list[str] = []
    seen: set[str] = set()
    for heading, body in _split_sections(text.replace("\r\n", "\n")):
        if heading and _END_SECTIONS.match(heading):
            break
        for paragraph in re.split(r"\n{2,}", body):
            subspecies = [m.group(0) for m in _TRINOMIAL_AUTHOR_RE.finditer(paragraph)]
            if kept and ((len(subspecies) >= 2
                          and sum(map(len, subspecies)) >= 0.12 * len(paragraph))
                         or len(_TRINOMIAL_ITEM_RE.findall(paragraph)) >= 2):
                kept[-1] = _drop_list_intro(kept[-1])
                continue
            cleaned = _clean_paragraph(paragraph, seen)
            if cleaned:
                kept.append(cleaned)
    if kept:
        kept[-1] = _drop_list_intro(kept[-1])
    return "\n\n".join(p for p in kept if p).strip()


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
