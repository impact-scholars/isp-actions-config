#!/usr/bin/env python3
"""Zenodo deposit helper for impact-scholars papers.

Subcommands: prepare, publish, status. See claude-plan.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from ruamel.yaml import YAML

ZENODO_PROD = "https://zenodo.org/api"
ZENODO_SANDBOX = "https://sandbox.zenodo.org/api"
PREFIX_PROD = "10.5281/zenodo."
PREFIX_SANDBOX = "10.5072/zenodo."

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def yaml_rt() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def load_myst(path: Path):
    y = yaml_rt()
    with path.open() as f:
        data = y.load(f)
    return y, data


def save_myst(path: Path, y: YAML, data) -> None:
    with path.open("w") as f:
        y.dump(data, f)


def api_base(sandbox: bool) -> str:
    return ZENODO_SANDBOX if sandbox else ZENODO_PROD


def doi_prefix(sandbox: bool) -> str:
    return PREFIX_SANDBOX if sandbox else PREFIX_PROD


def is_sandbox_doi(doi: str) -> bool:
    return doi.startswith(PREFIX_SANDBOX)


def emit_result(status: str, **kwargs) -> None:
    """Emit the run's outcome as a single JSON object on stdout.

    Each subcommand must call emit_result exactly once before returning.

    success: {"status": "ok",    "version_doi": ..., "draft_url": ..., ...domain fields}
    failure: {"status": "error", "message": "<one-line or markdown>", ...optional context}
    """
    print(json.dumps({"status": status, **kwargs}, indent=2))


def request(method: str, url: str, token: str, **kw) -> requests.Response:
    params = kw.pop("params", None) or {}
    params.setdefault("access_token", token)
    r = requests.request(method, url, params=params, timeout=60, **kw)
    if not r.ok:
        sys.stderr.write(
            f"\n[zenodo {method} {url}] {r.status_code}\n  {r.text[:2000]}\n"
        )
        r.raise_for_status()
    return r


# Raw `html` nodes are intentionally excluded so author-embedded markup is dropped.
_TEXT_LEAVES = ("text", "inlineMath", "inlineCode")


def _flatten_text(node, buf: list) -> None:
    """Concatenate text under an mdast node. inlineMath/inlineCode contribute their source."""
    if node.get("type") in _TEXT_LEAVES:
        buf.append(node.get("value") or "")
        return
    for child in node.get("children") or []:
        _flatten_text(child, buf)


def abstract_paragraphs(repo_root: Path) -> list[str] | None:
    """Plain-text abstract paragraphs from the MyST HTML build, if present.

    `myst build --html` parses the `abstract:` frontmatter into an mdast tree at
    frontmatter.parts.abstract.mdast in _build/site/content/<page>.json. Zenodo
    descriptions don't render math or markup, so we flatten to text and let the
    caller HTML-escape and wrap each paragraph in <p>. Returns None when no build
    artifact exists (e.g. at prepare time, which doesn't build the paper).
    """
    content_dir = repo_root / "_build" / "site" / "content"
    if not content_dir.is_dir():
        return None
    for jf in sorted(content_dir.glob("*.json")):
        try:
            data = json.loads(jf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        part = ((data.get("frontmatter") or {}).get("parts") or {}).get("abstract")
        mdast = part.get("mdast") if isinstance(part, dict) else part
        if not isinstance(mdast, dict):
            continue
        paras: list[str] = []

        def visit(node):
            if node.get("type") == "paragraph":
                buf: list = []
                _flatten_text(node, buf)
                text = "".join(buf).strip()
                if text:
                    paras.append(text)
                return  # don't descend past a paragraph
            for child in node.get("children") or []:
                visit(child)

        visit(mdast)
        if not paras:  # no paragraph wrapper: flatten the whole subtree
            buf: list = []
            _flatten_text(mdast, buf)
            text = "".join(buf).strip()
            if text:
                paras.append(text)
        if paras:
            return paras
    return None


def build_metadata(
    myst,
    *,
    github_url,
    site_url,
    version=None,
    publication_date=None,
    abstract_paras=None,
):
    project = myst["project"]
    creators = []
    for a in project.get("authors") or []:
        c = {"name": str(a["name"])}
        affs = a.get("affiliations") or []
        if affs:
            c["affiliation"] = str(affs[0])
        orcid = str(a.get("orcid") or "")
        # Zenodo looks up the ORCID profile and clobbers `name` if it doesn't resolve;
        # the micropub template ships placeholder ORCIDs (0000-0000-...) that hit this.
        if orcid and ORCID_RE.match(orcid) and not orcid.startswith("0000-0000-"):
            c["orcid"] = orcid
        elif orcid:
            sys.stderr.write(
                f"[warn] skipping invalid/placeholder ORCID for {a['name']}: {orcid}\n"
            )
        creators.append(c)

    keywords = [str(k) for k in project.get("keywords") or []]
    license_id = str(project.get("license") or "cc-by-4.0").lower()

    desc = []
    if abstract_paras:
        desc.extend(f"<p>{html.escape(p)}</p>" for p in abstract_paras)
    desc.append(
        "<p>This micropublication was created as part of the Neuromatch "
        "Impact Scholars Program 2025.</p>"
    )
    if site_url:
        desc.append(f'<p>Project Website: <a href="{site_url}">{site_url}</a></p>')
    desc.append(f'<p>Repository: <a href="{github_url}">{github_url}</a></p>')
    venue = project.get("venue")
    if venue:
        v = (
            venue
            if isinstance(venue, str)
            else (venue.get("title") if hasattr(venue, "get") else str(venue))
        )
        desc.append(f"<p>Venue: {v}</p>")
    funding = project.get("funding")
    if funding:
        desc.append(f"<p>Funding: {funding}</p>")

    related = [{"identifier": github_url, "relation": "isVersionOf", "scheme": "url"}]
    if site_url:
        related.append(
            {"identifier": site_url, "relation": "isIdenticalTo", "scheme": "url"}
        )

    md = {
        "upload_type": "publication",
        "publication_type": "article",
        "title": str(project["title"]),
        "creators": creators,
        "description": "".join(desc),
        "license": license_id,
        "related_identifiers": related,
        "access_right": "open",
        "communities": [{"identifier": "neuromatch"}],
    }
    if keywords:
        md["keywords"] = keywords
    if version is not None:
        md["version"] = version
    pubdate = publication_date or project.get("date")
    if pubdate:
        md["publication_date"] = str(pubdate)
    return md


def list_my_depositions(api: str, token: str, *, q: str | None = None) -> list:
    params = {"size": 100}
    if q:
        params["q"] = q
    r = request("GET", f"{api}/deposit/depositions", token, params=params)
    return r.json()


def find_by_github(api: str, token: str, github_url: str) -> dict | None:
    items = list_my_depositions(api, token, q=f'related.identifier:"{github_url}"')
    for it in items:
        for ri in it.get("metadata", {}).get("related_identifiers") or []:
            if ri.get("identifier") == github_url:
                return it
    if not items:
        for it in list_my_depositions(api, token):
            for ri in it.get("metadata", {}).get("related_identifiers") or []:
                if ri.get("identifier") == github_url:
                    return it
    return None


def get_deposition(api: str, token: str, dep_id) -> dict:
    return request("GET", f"{api}/deposit/depositions/{dep_id}", token).json()


def update_metadata(api: str, token: str, dep_id, metadata: dict) -> dict:
    r = request(
        "PUT",
        f"{api}/deposit/depositions/{dep_id}",
        token,
        json={"metadata": metadata},
        headers={"Content-Type": "application/json"},
    )
    return r.json()


def upload_file(bucket_url: str, token: str, path: Path) -> dict:
    with path.open("rb") as f:
        r = requests.put(
            f"{bucket_url}/{path.name}",
            params={"access_token": token},
            data=f,
            timeout=600,
        )
    if not r.ok:
        sys.stderr.write(f"\n[upload {path.name}] {r.status_code} {r.text[:1000]}\n")
        r.raise_for_status()
    return r.json()


def concept_doi_for(dep: dict, sandbox: bool) -> str:
    # `conceptdoi` is only set after first publish; before that, build from `conceptrecid`.
    return dep.get("conceptdoi") or f"{doi_prefix(sandbox)}{dep['conceptrecid']}"


SUPP_PATTERNS = ("*.csv", "*.png", "*.txt", "*.zip", "*.bib")


def repo_from_github_url(url: str) -> str:
    return url.rstrip("/").split("github.com/", 1)[-1]


def git_head_sha(repo_root: Path) -> str:
    return (
        subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
        .decode()
        .strip()
    )


def discover_review_pr(repo_root: Path) -> str | None:
    log = subprocess.check_output(
        ["git", "-C", str(repo_root), "log", "-20", "--pretty=%s"]
    ).decode()
    for line in log.splitlines():
        m = re.search(r"#(\d+)", line)
        if m:
            return m.group(1)
    return None


def build_bundle(
    out: Path, pdf: Path, repo_root: Path, *, provenance: dict
) -> list[Path]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy2(pdf, out / "paper.pdf")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "archive",
            "--format=zip",
            "-o",
            str((out / "source.zip").resolve()),
            "HEAD",
        ],
        check=True,
    )
    for pat in SUPP_PATTERNS:
        for p in sorted(repo_root.glob(pat)):
            if p.is_file() and p.name != "source.zip":
                shutil.copy2(p, out / p.name)
    myst_src = repo_root / "myst.yml"
    if myst_src.exists():
        shutil.copy2(myst_src, out / "myst.yml")
    (out / "publication-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    return sorted(p for p in out.iterdir() if p.is_file())


def latest_version_dep_id(api: str, token: str, concept_doi: str) -> str | None:
    items = list_my_depositions(api, token, q=f'conceptdoi:"{concept_doi}"')
    if not items:
        m = re.search(r"zenodo\.(\d+)", concept_doi)
        if m:
            items = list_my_depositions(api, token, q=f"conceptrecid:{m.group(1)}")
    if not items:
        return None
    items.sort(key=lambda d: d.get("created", ""), reverse=True)
    return items[0]["id"]


def cmd_prepare(args) -> int:
    myst_path = Path(args.myst)
    y, myst = load_myst(myst_path)
    project = myst["project"]

    if project.get("doi"):
        emit_result(
            "error",
            message=(
                f"project.doi already set ({project['doi']}); prepare is for first deposit."
            ),
        )
        return 2

    github_url = f"https://github.com/{args.repo}"
    api = api_base(args.sandbox)

    md = build_metadata(
        myst,
        github_url=github_url,
        site_url=args.site_url,
        abstract_paras=abstract_paragraphs(myst_path.resolve().parent),
    )
    md["prereserve_doi"] = True

    existing = find_by_github(api, args.token, github_url)
    if existing and existing.get("submitted") is False:
        sys.stderr.write(f"[prepare] reusing draft {existing['id']}\n")
        dep = update_metadata(api, args.token, existing["id"], md)
    elif existing:
        emit_result(
            "error",
            message=(
                f"Published deposit already exists ({existing['id']}); refusing to create a "
                f"parallel concept. Add its DOI to myst.yml manually."
            ),
        )
        return 3
    else:
        r = request(
            "POST",
            f"{api}/deposit/depositions",
            args.token,
            json={"metadata": md},
            headers={"Content-Type": "application/json"},
        )
        dep = r.json()

    cdoi = concept_doi_for(dep, args.sandbox)
    draft_url = dep.get("links", {}).get("html")

    project["doi"] = cdoi
    project["github"] = github_url
    save_myst(myst_path, y, myst)

    emit_result(
        "ok",
        concept_doi=cdoi,
        draft_url=draft_url,
        deposition_id=dep["id"],
    )
    return 0


def cmd_publish(args) -> int:
    myst_path = Path(args.myst)
    _, myst = load_myst(myst_path)
    project = myst["project"]

    concept_doi = project.get("doi")
    if not concept_doi:
        emit_result(
            "error",
            message="project.doi missing — run prepare and merge that PR before tagging.",
        )
        return 2
    if is_sandbox_doi(concept_doi) != args.sandbox:
        emit_result(
            "error",
            message=(
                f"DOI prefix says sandbox={is_sandbox_doi(concept_doi)} but --sandbox={args.sandbox}."
            ),
        )
        return 2

    api = api_base(args.sandbox)
    tag = args.tag
    if not re.match(r"^v\d+\.\d+\.\d+$", tag):
        emit_result("error", message=f"tag must match vMAJOR.MINOR.PATCH (got {tag})")
        return 2
    version = tag[1:]

    pdf = Path(args.pdf)
    if not pdf.is_file():
        emit_result("error", message=f"--pdf not found: {pdf}")
        return 2

    github_url = project.get("github")
    if not github_url:
        emit_result(
            "error", message="project.github missing — should have been set by prepare."
        )
        return 2

    latest_id = latest_version_dep_id(api, args.token, concept_doi)
    if latest_id is None:
        emit_result(
            "error",
            message=(
                f"No Zenodo record matches {concept_doi} (token mismatch? deleted draft?)"
            ),
        )
        return 2

    dep = get_deposition(api, args.token, latest_id)

    expected_concept = f"{doi_prefix(args.sandbox)}{dep['conceptrecid']}"
    if concept_doi != expected_concept:
        emit_result(
            "error",
            message=(
                f"Concept DOI sanity check failed: myst.yml has {concept_doi}, "
                f"Zenodo's conceptrecid implies {expected_concept}"
            ),
        )
        return 2

    if dep.get("submitted"):
        # `deposit:actions` (which newversion needs) is intentionally not granted to the
        # CI token; an editor must click "New version" on Zenodo to spawn the empty
        # draft, then re-run the workflow.
        record_url = dep.get("links", {}).get("record_html") or dep.get(
            "links", {}
        ).get("html")
        print(
            f"::error title=Zenodo: editor must click 'New version'::No unsubmitted "
            f"draft for this concept. Record: {record_url or '(unavailable)'}",
            file=sys.stderr,
        )
        emit_result(
            "error",
            message=(
                f"No unsubmitted Zenodo draft exists for this concept DOI.\n\n"
                f"An editor must open the record and click **New version** to spawn an empty draft, "
                f"then re-run failed jobs.\n\n"
                f"Record: {record_url or '(URL unavailable)'}"
            ),
            record_url=record_url,
        )
        return 5
    sys.stderr.write(f"[publish] reusing existing draft {dep['id']}\n")

    repo_root = myst_path.resolve().parent
    md = build_metadata(
        myst,
        github_url=github_url,
        site_url=args.site_url,
        version=version,
        publication_date=str(project.get("date") or dt.date.today().isoformat()),
        abstract_paras=abstract_paragraphs(repo_root),
    )
    dep = update_metadata(api, args.token, dep["id"], md)

    bucket = dep.get("links", {}).get("bucket")
    if not bucket:
        emit_result(
            "error",
            message="No bucket URL in deposition (unexpected Zenodo API shape).",
        )
        return 4

    # Predicted from the deposition id; matches what Zenodo assigns at publish.
    predicted_version_doi = f"{doi_prefix(args.sandbox)}{dep['id']}"
    provenance = {
        "repo": repo_from_github_url(github_url),
        "commit_sha": git_head_sha(repo_root),
        "tag": tag,
        "site_url": args.site_url,
        "concept_doi": concept_doi,
        "version_doi": predicted_version_doi,
        "review_pr": discover_review_pr(repo_root),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    bundle = Path(args.bundle_out)
    files = build_bundle(bundle, pdf, repo_root, provenance=provenance)

    for p in files:
        sys.stderr.write(f"[publish] upload {p.name}\n")
        upload_file(bucket, args.token, p)

    dep = get_deposition(api, args.token, dep["id"])
    version_doi = (
        dep.get("metadata", {}).get("doi") or dep.get("doi") or predicted_version_doi
    )
    emit_result(
        "ok",
        version_doi=version_doi,
        draft_url=dep.get("links", {}).get("html"),
        deposition_id=dep["id"],
        bundle_dir=str(bundle),
    )
    return 0


def cmd_status(args) -> int:
    myst_path = Path(args.myst)
    _, myst = load_myst(myst_path)
    project = myst["project"]
    concept_doi = project.get("doi")
    api = api_base(args.sandbox)

    out = {
        "myst_path": str(myst_path),
        "concept_doi": concept_doi,
        "github": project.get("github"),
    }

    if not concept_doi:
        out["state"] = "no doi yet — prepare not run or PR not merged"
        print(json.dumps(out, indent=2))
        return 0

    if is_sandbox_doi(concept_doi) != args.sandbox:
        out["warning"] = (
            f"DOI prefix vs --sandbox mismatch (doi={concept_doi}, sandbox={args.sandbox})"
        )

    latest_id = latest_version_dep_id(api, args.token, concept_doi)
    if latest_id is None:
        out["state"] = "no record matches"
        print(json.dumps(out, indent=2))
        return 0

    dep = get_deposition(api, args.token, latest_id)
    out["latest_deposition_id"] = dep["id"]
    out["submitted"] = dep.get("submitted", False)
    out["draft_url"] = dep.get("links", {}).get("html")
    out["latest_version"] = dep.get("metadata", {}).get("version")
    out["latest_doi"] = dep.get("metadata", {}).get("doi") or dep.get("doi")

    md_preview = build_metadata(
        myst,
        github_url=project.get("github") or "",
        site_url=args.site_url or "",
        abstract_paras=abstract_paragraphs(myst_path.resolve().parent),
    )
    out["metadata_preview_keys"] = sorted(md_preview.keys())
    out["creator_count"] = len(md_preview.get("creators", []))
    out["description_preview"] = md_preview.get("description", "")

    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="zenodo-deposit")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--myst", default="myst.yml")
    common.add_argument("--token")
    common.add_argument("--sandbox", action="store_true")

    pp = sub.add_parser("prepare", parents=[common])
    pp.add_argument("--repo", required=True, help="owner/repo on GitHub")
    pp.add_argument("--site-url", required=True)
    pp.add_argument(
        "--version", help="kept for parity with workflow input; unused at prepare"
    )
    pp.set_defaults(func=cmd_prepare)

    pu = sub.add_parser("publish", parents=[common])
    pu.add_argument("--pdf", required=True, help="path to built paper.pdf")
    pu.add_argument("--tag", required=True)
    pu.add_argument("--site-url", required=True)
    pu.add_argument(
        "--bundle-out", default="_bundle", help="dir to assemble (debugging)"
    )
    pu.set_defaults(func=cmd_publish)

    ps = sub.add_parser("status", parents=[common])
    ps.add_argument("--site-url", default="")
    ps.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    if not args.token:
        # Caller workflow is responsible for selecting prod vs sandbox secret and
        # injecting it as ZENODO_TOKEN. The script no longer reads two env vars.
        args.token = os.environ.get("ZENODO_TOKEN")
    if not args.token:
        sys.stderr.write("no token: set ZENODO_TOKEN or pass --token\n")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
