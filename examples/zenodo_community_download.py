#!/usr/bin/env python3
"""
Download metadata (YAML) and .pptx files for the most recent versions of all
records in a Zenodo community.

Usage:
    export ZENODO_API_KEY="..."
    python zenodo_community_download.py my-community-slug \
        --meta-dir out/meta --pptx-dir out/pptx

Requirements:
    pip install requests pyyaml
"""

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import requests
import yaml

API_BASE = "https://zenodo.org/api"
PAGE_SIZE = 100


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    session = requests.Session()
    token = os.environ.get("ZENODO_API_KEY")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        print("Warning: ZENODO_API_KEY not set, continuing unauthenticated "
              "(lower rate limits).", file=sys.stderr)
    session.headers["User-Agent"] = "zenodo-community-downloader/1.0"
    return session


def get(session: requests.Session, url: str, retries: int = 5, **kwargs):
    """GET with simple retry/backoff, honouring 429 Retry-After."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=60, **kwargs)
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"  Request error ({exc}); retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  Rate limited; waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code >= 500 and attempt < retries:
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Failed to GET {url}")


# --------------------------------------------------------------------------- #
# Record iteration
# --------------------------------------------------------------------------- #
def iter_community_records(session: requests.Session, community: str):
    """
    Yield every record of the community. Zenodo's search returns only the
    latest version of each record unless `allversions=true` is passed, so we
    deliberately do not pass it.
    """
    url = f"{API_BASE}/communities/{community}/records"
    params = {"size": PAGE_SIZE, "page": 1, "sort": "newest"}
    while url:
        data = get(session, url, params=params).json()
        for hit in data.get("hits", {}).get("hits", []):
            yield hit
        url = data.get("links", {}).get("next")
        params = None  # the "next" link already carries the query string


# --------------------------------------------------------------------------- #
# Metadata extraction (tolerates both legacy and InvenioRDM-style JSON)
# --------------------------------------------------------------------------- #
def _affiliations(creator: dict) -> list[str]:
    aff = creator.get("affiliations", creator.get("affiliation"))
    if not aff:
        return []
    if isinstance(aff, str):
        return [aff]
    result = []
    for a in aff:
        if isinstance(a, dict):
            name = a.get("name") or a.get("id")
            if name:
                result.append(name)
        elif a:
            result.append(str(a))
    return result


def extract_authors(metadata: dict) -> list[dict]:
    authors = []
    for c in metadata.get("creators", []):
        name = c.get("name") or c.get("person_or_org", {}).get("name")
        authors.append({"name": name, "affiliations": _affiliations(c)})
    return authors


def extract_license(metadata: dict):
    lic = metadata.get("license")
    if isinstance(lic, dict):
        title = lic.get("title")
        if isinstance(title, dict):  # InvenioRDM: {"en": "..."}
            title = title.get("en") or next(iter(title.values()), None)
        return {"id": lic.get("id"), "title": title}
    if lic:
        return {"id": str(lic), "title": None}
    # InvenioRDM style: "rights": [{id, title}]
    rights = metadata.get("rights")
    if rights:
        r = rights[0]
        title = r.get("title")
        if isinstance(title, dict):
            title = title.get("en") or next(iter(title.values()), None)
        return {"id": r.get("id"), "title": title}
    return None


def build_record_info(record: dict) -> dict:
    md = record.get("metadata", {})
    return {
        "id": record.get("id"),
        "doi": record.get("doi") or md.get("doi"),
        "url": record.get("links", {}).get("self_html"),
        "title": md.get("title"),
        "authors": extract_authors(md),
        "license": extract_license(md),
        "description": md.get("description"),
    }


# --------------------------------------------------------------------------- #
# YAML output
# --------------------------------------------------------------------------- #
def _str_presenter(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _str_presenter)


def write_yaml(path: Path, info: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.dump(info, fh, allow_unicode=True, sort_keys=False,
                  default_flow_style=False, width=100)
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #
def iter_files(record: dict):
    """Yield (filename, download_url, checksum_or_None) for each file."""
    rec_id = record.get("id")
    files = record.get("files", [])
    if isinstance(files, dict):  # InvenioRDM style
        files = list(files.get("entries", {}).values())
    for f in files:
        key = f.get("key") or f.get("filename")
        if not key:
            continue
        links = f.get("links", {})
        url = (links.get("self") or links.get("download")
               or f"{API_BASE}/records/{rec_id}/files/{key}/content")
        yield key, url, f.get("checksum")


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(session: requests.Session, url: str, dest: Path,
                  checksum: str | None) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with get(session, url, stream=True) as resp, open(tmp, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)

    if checksum and checksum.startswith("md5:"):
        expected = checksum.split(":", 1)[1]
        actual = md5_of(tmp)
        if actual != expected:
            tmp.unlink(missing_ok=True)
            raise IOError(f"Checksum mismatch for {dest.name} "
                          f"(expected {expected}, got {actual})")
    tmp.replace(dest)


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("community", help="Community slug or ID")
    parser.add_argument("--meta-dir", type=Path, default=Path("zenodo_meta"),
                        help="Folder for YAML files (default: zenodo_meta)")
    parser.add_argument("--pptx-dir", type=Path, default=Path("zenodo_pptx"),
                        help="Folder for .pptx files (default: zenodo_pptx)")
    args = parser.parse_args()

    args.meta_dir.mkdir(parents=True, exist_ok=True)
    args.pptx_dir.mkdir(parents=True, exist_ok=True)

    session = make_session()
    stats = {"records": 0, "yml_new": 0, "yml_skipped": 0,
             "pptx_new": 0, "pptx_skipped": 0, "errors": 0}

    for record in iter_community_records(session, args.community):
        rec_id = record.get("id")
        stats["records"] += 1
        title = (record.get("metadata", {}).get("title") or "")[:70]
        print(f"[{rec_id}] {title}")

        # --- metadata ---------------------------------------------------- #
        yml_path = args.meta_dir / f"{rec_id}.yml"
        if yml_path.exists():
            stats["yml_skipped"] += 1
            print("  yml exists, skipping")
        else:
            try:
                write_yaml(yml_path, build_record_info(record))
                stats["yml_new"] += 1
                print(f"  wrote {yml_path}")
            except Exception as exc:
                stats["errors"] += 1
                print(f"  ERROR writing yml: {exc}", file=sys.stderr)

        # --- pptx files -------------------------------------------------- #
        for key, url, checksum in iter_files(record):
            if not key.lower().endswith(".pptx"):
                continue
            dest = args.pptx_dir / f"{rec_id}_{safe_name(key)}"
            if dest.exists():
                stats["pptx_skipped"] += 1
                print(f"  {dest.name} exists, skipping")
                continue
            try:
                print(f"  downloading {key} ...")
                download_file(session, url, dest, checksum)
                stats["pptx_new"] += 1
            except Exception as exc:
                stats["errors"] += 1
                print(f"  ERROR downloading {key}: {exc}", file=sys.stderr)

    print("\nDone.")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()