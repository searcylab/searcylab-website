#!/usr/bin/env python3
"""
Generate one folder per BibTeX entry under publications/, each with index.qmd.

By default skips ATLAS-related rows: "ATLAS" in title or any author string, or
surname Aad / Aaboud (typical ATLAS paper author lines). Use --no-atlas-filter
to emit everything.

Requires: pip install -r scripts/requirements-publications.txt

Usage:
  python _utils/generate_publications_from_bib.py
  python _utils/generate_publications_from_bib.py --bib publications/citations.bib --out publications
  python _utils/generate_publications_from_bib.py --dry-run --limit 5
  python _utils/generate_publications_from_bib.py --force   # overwrite existing index.qmd
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_MONTH_ABBR = {
    "jan": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "may": "05",
    "jun": "06",
    "jul": "07",
    "aug": "08",
    "sep": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}

import bibtexparser
import yaml
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import author, convert_to_unicode

# Do not clobber these existing publication subdirs when the cite key matches.
RESERVED_DIR_NAMES = frozenset({"example", "_template"})

# Same keys and order as publications/example/index.qmd (Quarto front matter).
EXAMPLE_FRONT_MATTER_KEYS: tuple[str, ...] = (
    "title",
    "date",
    "author",
    "publication",
    "categories",
    "url_source",
    "url_preprint",
    "journ",
    "issue",
    "page",
    "year",
    "image",
    "pub_number",
)


class FlowStyleList(list):
    """YAML list rendered inline, matching example author: [\"a\", \"b\"] style."""


def _represent_flow_list(dumper: yaml.Dumper, data: FlowStyleList) -> yaml.nodes.Node:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", list(data), flow_style=True)


class PageField(str):
    """Always double-quoted in YAML so values like 094033 are not parsed as octal."""


def _represent_page_field(dumper: yaml.Dumper, data: PageField) -> yaml.nodes.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


for _Dumper in (yaml.SafeDumper, yaml.Dumper):
    yaml.add_representer(FlowStyleList, _represent_flow_list, Dumper=_Dumper)
    yaml.add_representer(PageField, _represent_page_field, Dumper=_Dumper)

ATLAS_WORD_RE = re.compile(r"\batlas\b", re.IGNORECASE)


def _author_raw_list(entry: dict) -> list[str]:
    a = entry.get("author")
    if not a:
        return []
    if isinstance(a, list):
        return [str(x) for x in a if x]
    return [str(a)]


def _family_names_from_bib_authors(authors: list[str]) -> list[str]:
    """Family name: text before comma, else last whitespace-delimited token."""
    out: list[str] = []
    for s in authors:
        s = s.strip()
        if not s:
            continue
        if "," in s:
            out.append(s.split(",", 1)[0].strip())
        else:
            parts = s.split()
            if parts:
                out.append(parts[-1].strip())
    return out


def excluded_atlas_collaboration(entry: dict) -> bool:
    """True if this row looks like an ATLAS / Aad-family paper (skipped by default)."""
    title = clean_title(entry.get("title", ""))
    if ATLAS_WORD_RE.search(title):
        return True

    raw_authors = _author_raw_list(entry)
    for s in raw_authors:
        if ATLAS_WORD_RE.search(s):
            return True

    for fam in _family_names_from_bib_authors(raw_authors):
        low = fam.lower()
        if low == "aad" or low.startswith("aaboud"):
            return True

    return False


def _make_parser() -> BibTexParser:
    p = BibTexParser(common_strings=True)
    p.ignore_nonstandard_types = False
    p.homogenise_fields = False

    def cust(r):
        r = convert_to_unicode(r)
        r = author(r)
        return r

    p.customization = cust
    return p


def clean_title(raw: str | None) -> str:
    if not raw:
        return ""
    t = raw
    t = re.sub(r"<\?[^>]*\?>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Drop common LaTeX wrappers (lightweight; titles may still contain math)
    t = t.replace("{", "").replace("}", "")
    return t.strip()


def author_to_display(name: str) -> str:
    name = name.strip().rstrip(",")
    if not name:
        return name
    low = name.lower()
    if low == "others" or low.startswith("others,"):
        return "others"
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name


def authors_yaml_list(author_field) -> list[str]:
    if not author_field:
        return []
    if isinstance(author_field, list):
        parts = author_field
    else:
        parts = [author_field]
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            # bibtexparser sometimes uses structured names
            given = " ".join(p.get("given", []) or [])
            family = " ".join(p.get("family", []) or [])
            if family or given:
                out.append(f"{given} {family}".strip())
            continue
        out.append(author_to_display(str(p)))
    return [a for a in out if a]


def journal_name(entry: dict) -> str:
    for k in ("journal", "journaltitle", "booktitle", "howpublished"):
        v = entry.get(k)
        if v:
            return clean_title(str(v))
    return ""


def build_publication_line(entry: dict) -> str:
    journ = journal_name(entry)
    year = (entry.get("year") or "").strip()
    vol = (entry.get("volume") or "").strip()
    num = (entry.get("number") or "").strip()
    pages = (entry.get("pages") or "").replace("--", "-").strip()
    issue_part = num or vol

    if journ:
        if issue_part and pages:
            return f"{journ}, **{issue_part}**, _{pages}_ ({year})."
        if issue_part:
            return f"{journ}, **{issue_part}** ({year})." if year else f"{journ}, **{issue_part}**."
        if pages:
            return f"{journ}, _{pages}_ ({year})." if year else f"{journ}, _{pages}_."
        return f"{journ} ({year})." if year else f"{journ}."
    return ""


def parse_issue_for_yaml(entry: dict) -> str | int | None:
    num = (entry.get("number") or "").strip()
    vol = (entry.get("volume") or "").strip()
    raw = num or vol
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    return raw


def _month_to_mm(month_raw: str) -> str:
    m = month_raw.strip().lower()
    if not m:
        return "01"
    if m.isdigit():
        v = int(m)
        if 1 <= v <= 12:
            return f"{v:02d}"
        return "01"
    key = m[:3]
    return _MONTH_ABBR.get(key, "01")


def entry_date_string(entry: dict, y: int | None) -> str:
    """ISO date for YAML `date:`; matches example style YYYY-MM-DD."""
    if y is None:
        return ""
    mm = _month_to_mm(str(entry.get("month") or ""))
    return f"{y}-{mm}-01"


def categories_from_entry(entry: dict) -> list[str]:
    raw = (entry.get("keywords") or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,;]\s*", raw)
    return [p.strip() for p in parts if p.strip()]


def url_source(entry: dict) -> str:
    doi = (entry.get("doi") or "").strip()
    if doi:
        d = doi
        if not d.lower().startswith("doi:") and "://" not in d:
            return f"https://doi.org/{d}"
        return d
    u = (entry.get("url") or "").strip()
    return u


def url_preprint(entry: dict) -> str:
    # arXiv-style fields (varies by exporter)
    eprint = (entry.get("eprint") or "").strip()
    archive = (entry.get("archiveprefix") or entry.get("eprinttype") or "").strip().lower()
    if eprint and ("arxiv" in archive or entry.get("journal", "").lower().startswith("arxiv")):
        e = eprint.replace("arXiv:", "").strip()
        return f"https://arxiv.org/abs/{e}"
    if eprint and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", eprint):
        return f"https://arxiv.org/abs/{eprint}"
    return ""


def sanitize_dir_name(key: str) -> str:
    key = key.strip()
    key = key.replace("/", "_").replace("\\", "_")
    return key or "unnamed_entry"


def unique_folder_name(cite_key: str, occurrence: int, year: int | None) -> str:
    """Directory name: ``{year}_{bibtag}`` (or ``0000_…`` if year missing); duplicate same year+key get ``__2``, …."""
    base = sanitize_dir_name(cite_key)
    if base in RESERVED_DIR_NAMES:
        base = f"{base}_bib"
    year_part = f"{year:04d}" if year is not None else "0000"
    slug = f"{year_part}_{base}"
    if occurrence <= 1:
        return slug
    return f"{slug}__{occurrence}"


def year_int(entry: dict) -> int | None:
    y = (entry.get("year") or "").strip()
    if not y or not y.isdigit():
        return None
    return int(y)


def publication_fallback(entry: dict, title: str, journ: str, y: int | None) -> str:
    """Human-readable citation line when journal/volume/pages are incomplete."""
    publisher = clean_title(entry.get("publisher") or "")
    if journ and y is not None:
        return f"{journ} ({y})."
    if journ:
        return f"{journ}."
    if publisher and y is not None:
        return f"{publisher} ({y})."
    if publisher:
        return f"{publisher}."
    if y is not None:
        return f"({y})."
    return title


def front_matter_dict(entry: dict, pub_number: int) -> dict[str, Any]:
    """All keys from publications/example/index.qmd, same order; empty strings / [] / null where unknown."""
    title = clean_title(entry.get("title", ""))
    journ = journal_name(entry)
    y = year_int(entry)
    page = (entry.get("pages") or "").replace("--", "-").strip()

    pub_line = build_publication_line(entry).strip()
    if not pub_line:
        pub_line = publication_fallback(entry, title, journ, y)

    authors = authors_yaml_list(entry.get("author"))
    cats = categories_from_entry(entry)
    issue_val = parse_issue_for_yaml(entry)

    page_yaml: str | PageField = PageField(page) if page else ""

    values: dict[str, Any] = {
        "title": title,
        "date": entry_date_string(entry, y),
        "author": FlowStyleList(authors),
        "publication": pub_line,
        "categories": FlowStyleList(cats),
        "url_source": url_source(entry),
        "url_preprint": url_preprint(entry),
        "journ": journ,
        "issue": issue_val,
        "page": page_yaml,
        "year": y,
        "image": "",
        "pub_number": int(pub_number),
    }

    ordered: dict[str, Any] = {}
    for k in EXAMPLE_FRONT_MATTER_KEYS:
        ordered[k] = values[k]
    return ordered


def dump_qmd_front_matter(fm: dict[str, Any]) -> str:
    lines = yaml.dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
        Dumper=yaml.SafeDumper,
    ).rstrip()
    return f"---\n{lines}\n---\n"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Generate publication folders from a .bib file.")
    ap.add_argument(
        "--bib",
        type=Path,
        default=root / "publications" / "citations.bib",
        help="Path to BibTeX file",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=root / "publications",
        help="Output directory (publication subfolders created here)",
    )
    ap.add_argument("--force", action="store_true", help="Overwrite existing index.qmd")
    ap.add_argument("--dry-run", action="store_true", help="Print actions only")
    ap.add_argument("--limit", type=int, default=0, help="Max entries to process (0 = all)")
    ap.add_argument(
        "--no-atlas-filter",
        action="store_true",
        help="Do not skip entries with ATLAS in title/authors or surname Aad/Aaboud",
    )
    args = ap.parse_args()

    bib_path: Path = args.bib
    out_dir: Path = args.out
    if not bib_path.is_file():
        print(f"error: bib file not found: {bib_path}", file=sys.stderr)
        return 1

    parser = _make_parser()
    with bib_path.open(encoding="utf-8", errors="replace") as f:
        db = bibtexparser.load(f, parser=parser)

    entries = db.entries
    total_in_bib = len(entries)

    if args.no_atlas_filter:
        kept = list(entries)
    else:
        kept = [e for e in entries if not excluded_atlas_collaboration(e)]

    skipped = total_in_bib - len(kept)
    total = len(kept)
    key_counts: dict[tuple[int | None, str], int] = defaultdict(int)

    planned: list[tuple[str, Path, dict]] = []
    for idx, entry in enumerate(kept):
        cite_key = entry.get("ID") or f"entry_{idx}"
        y = year_int(entry)
        dedupe_key = (y, cite_key)
        key_counts[dedupe_key] += 1
        occ = key_counts[dedupe_key]
        folder_name = unique_folder_name(cite_key, occ, y)
        # Listing sort is pub_number desc: first row in `kept` gets N, then N-1, …, 1 (no gaps).
        pub_number = total - idx
        fm = front_matter_dict(entry, pub_number)
        dest_dir = out_dir / folder_name
        planned.append((cite_key, dest_dir, fm))

    n = 0
    for cite_key, dest_dir, fm in planned:
        if args.limit and n >= args.limit:
            break
        qmd = dest_dir / "index.qmd"
        if qmd.exists() and not args.force:
            if args.dry_run:
                print(f"skip exists: {qmd}")
            n += 1
            continue
        if args.dry_run:
            print(f"would write: {qmd} (key={cite_key})")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            qmd.write_text(dump_qmd_front_matter(fm), encoding="utf-8")
        n += 1

    filter_note = ""
    if not args.no_atlas_filter:
        filter_note = f", skipped {skipped} ATLAS/Aad/Aaboud-related"
    print(
        f"Processed {n} entr{'y' if n == 1 else 'ies'} "
        f"(of {total} kept from {total_in_bib} in bib{filter_note}; "
        f"dry_run={args.dry_run}, force={args.force})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
