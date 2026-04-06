#!/usr/bin/env python3
"""
Generate one folder per BibTeX entry under publications/, each with index.qmd.

Requires: pip install -r scripts/requirements-publications.txt

Usage:
  python scripts/generate_publications_from_bib.py
  python scripts/generate_publications_from_bib.py --bib publications/citations.bib --out publications
  python scripts/generate_publications_from_bib.py --dry-run --limit 5
  python scripts/generate_publications_from_bib.py --force   # overwrite existing index.qmd
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import bibtexparser
import yaml
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import author, convert_to_unicode

# Do not clobber these existing publication subdirs when the cite key matches.
RESERVED_DIR_NAMES = frozenset({"example", "_template"})


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


def unique_folder_name(cite_key: str, occurrence: int) -> str:
    base = sanitize_dir_name(cite_key)
    if base in RESERVED_DIR_NAMES:
        base = f"{base}_bib"
    if occurrence <= 1:
        return base
    return f"{base}__{occurrence}"


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


def front_matter_dict(entry: dict, pub_number: int) -> dict:
    title = clean_title(entry.get("title", ""))
    journ = journal_name(entry)
    y = year_int(entry)
    page = (entry.get("pages") or "").replace("--", "-").strip()

    pub_line = build_publication_line(entry).strip()
    if not pub_line:
        pub_line = publication_fallback(entry, title, journ, y)

    fm: dict = {
        "title": title,
        "author": authors_yaml_list(entry.get("author")),
        "publication": pub_line,
        "url_source": url_source(entry) or None,
        "url_preprint": url_preprint(entry) or None,
        "journ": journ or None,
        "issue": parse_issue_for_yaml(entry),
        "page": page or None,
        "year": y,
        "image": "",
        "pub_number": pub_number,
    }
    if y is not None:
        fm["date"] = f"{y}-01-01"

    # Drop None values for cleaner YAML (Quarto treats missing keys as empty)
    return {k: v for k, v in fm.items() if v is not None}


def dump_qmd_front_matter(fm: dict) -> str:
    lines = yaml.dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
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
    total = len(entries)
    key_counts: dict[str, int] = defaultdict(int)

    planned: list[tuple[str, Path, dict]] = []
    for idx, entry in enumerate(entries):
        cite_key = entry.get("ID") or f"entry_{idx}"
        key_counts[cite_key] += 1
        occ = key_counts[cite_key]
        folder_name = unique_folder_name(cite_key, occ)
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

    print(
        f"Processed {n} entr{'y' if n == 1 else 'ies'} "
        f"(of {total} in bib; dry_run={args.dry_run}, force={args.force})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
