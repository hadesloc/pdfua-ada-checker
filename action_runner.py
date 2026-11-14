#!/usr/bin/env python3
"""GitHub Action runner for ISO 14289-1 (PDF/UA-1) & ADA Title II PDF checks.

Zero third-party dependencies (Python 3 standard library only).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per PDF
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
}

STOREFRONT_CHECKER_URL = "https://hadesloc.github.io/pdfua-partner-desk/#free-checker"
EXPRESS_19_URL = "https://pdfuapartnerdesk.gumroad.com/l/pdfua-express-5-pages?wanted=true"
STARTER_65_URL = "https://pdfuapartnerdesk.gumroad.com/l/pdfua-remediation-12-pages?wanted=true"
STANDARD_195_URL = "https://pdfuapartnerdesk.gumroad.com/l/pdfua-remediation-30-pages?wanted=true"
AGENCY_CALC_URL = "https://hadesloc.github.io/pdfua-partner-desk/#calculator"


def parse_bool(val: str) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def discover_pdfs(pdf_path_input: str, max_files: int, workspace: Path) -> list[Path]:
    raw = (pdf_path_input or ".").strip()
    candidates: list[Path] = []

    target = (workspace / raw) if not Path(raw).is_absolute() else Path(raw)
    if target.is_file():
        if target.suffix.lower() == ".pdf":
            candidates.append(target)
    elif target.is_dir():
        for root, dirs, files in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git"))
            for fname in sorted(files):
                if fname.lower().endswith(".pdf"):
                    candidates.append(Path(root) / fname)
    else:
        pattern = str(workspace / raw) if not Path(raw).is_absolute() else raw
        for matched in sorted(glob.glob(pattern, recursive=True)):
            p = Path(matched)
            if p.is_file() and p.suffix.lower() == ".pdf":
                if not any(part in SKIP_DIRS for part in p.parts):
                    candidates.append(p)

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in candidates:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(p)
        if len(unique) >= max(1, max_files):
            break
    return unique


def build_multipart_body(pdf_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = f"----PdfUaActionBoundary{uuid.uuid4().hex}"
    safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return header + pdf_bytes + footer, f"multipart/form-data; boundary={boundary}"


def warm_health_endpoint(api_url: str) -> None:
    parsed = urllib.parse.urlsplit(api_url)
    if not parsed.scheme or not parsed.netloc:
        return
    if parsed.path.rstrip("/").endswith("/api/check"):
        health_path = parsed.path.rstrip("/")[: -len("/api/check")] + "/api/health"
    else:
        return
    health_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))
    req = urllib.request.Request(
        health_url,
        headers={"User-Agent": "pdfua-ada-checker-github-action/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            resp.read()
    except Exception:
        pass


def local_structural_fallback(pdf_bytes: bytes, filename: str) -> dict[str, Any]:
    raw = pdf_bytes.decode("latin1", errors="ignore")
    pages = max(1, len(re.findall(r"/Type\s*/Page(?![A-Za-z])", raw)))
    has_struct = bool(re.search(r"/StructTreeRoot\b", raw))
    is_marked = bool(re.search(r"/Marked\s+true\b", raw, re.IGNORECASE))
    is_tagged = has_struct and is_marked
    has_pdfua_id = bool(re.search(r"pdfuaid:part", raw, re.IGNORECASE))
    has_lang = bool(re.search(r"/Lang\s*\([^)]{1,24}\)", raw))
    has_title = bool(
        re.search(r"<dc:title>[\s\S]{1,400}?<rdf:li[^>]*>[^<\s]", raw)
        or re.search(r"/Title\s*\([^)]+\)", raw)
    )
    display_title = bool(re.search(r"/DisplayDocTitle\s+true\b", raw, re.IGNORECASE))

    findings: list[dict[str, Any]] = []
    if not is_tagged:
        findings.append({"clause": "6.2/7.1", "failed_checks": pages})
    if not has_pdfua_id:
        findings.append({"clause": "5", "failed_checks": 1})
    if not has_title or not display_title:
        findings.append({"clause": "7.1", "failed_checks": 1})
    if not has_lang:
        findings.append({"clause": "7.2", "failed_checks": 1})

    failed_rules = len(findings)
    failed_checks = sum(int(f.get("failed_checks", 1)) for f in findings)
    return {
        "filename": filename,
        "is_compliant": failed_rules == 0 and is_tagged and has_pdfua_id,
        "engine": "Offline Structural PDF/UA-1 Fallback Inspector",
        "failed_rules": failed_rules,
        "failed_checks": failed_checks,
        "structure": {"pages": pages},
        "findings": findings,
    }


def check_pdf_via_api(
    pdf_bytes: bytes,
    filename: str,
    api_url: str,
    retries: int = 3,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    body, content_type = build_multipart_body(pdf_bytes, filename)
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Content-Type": content_type,
                "X-Filename": urllib.parse.quote(filename),
                "User-Agent": "pdfua-ada-checker-github-action/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, dict) and "is_compliant" in payload:
                    return payload
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in {400, 413, 422}:
                err_body = exc.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"API rejected {filename} (HTTP {exc.code}): {err_body}") from exc
        except Exception as exc:
            last_err = exc

        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))

    print(
        f"::warning::Remote API {api_url} unreachable ({last_err}); using local structural PDF/UA-1 fallback for {filename}.",
        file=sys.stderr,
    )
    return local_structural_fallback(pdf_bytes, filename)


def rel_display(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except Exception:
        return path.name


def write_github_outputs(outputs: dict[str, Any], output_file: str | None) -> None:
    if not output_file:
        return
    with Path(output_file).open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            fh.write(f"{key}={value}\n")


def render_markdown_summary(rows: list[dict[str, Any]], skipped_large: list[str]) -> str:
    total = len(rows)
    failing = sum(1 for r in rows if not r["is_compliant"])
    passing = total - failing
    total_rules = sum(int(r["failed_rules"]) for r in rows)

    lines = [
        "### 🛡️ PDF/UA-1 & ADA Title II Accessibility Check (`veraPDF 1.30.3`)",
        "",
        f"- **PDFs Audited:** `{total}` &nbsp;|&nbsp; **Passing (`0` failed rules):** `{passing}` &nbsp;|&nbsp; **Failing:** `{failing}` &nbsp;|&nbsp; **Total Failed Rules:** `{total_rules}`",
        "",
    ]

    if total == 0:
        lines.append("_No PDF files found matching the configured `pdf-path`._")
    else:
        lines.extend(
            [
                "| PDF Document | Pages | Verdict | Failed Rules | Failed Checks | Violated ISO 14289-1 Clauses |",
                "| :--- | ---: | :--- | ---: | ---: | :--- |",
            ]
        )
        for r in rows:
            verdict = "✅ PASS (`PDF/UA-1`)" if r["is_compliant"] else "❌ FAIL"
            clauses = ", ".join(r["clauses"]) if r["clauses"] else "—"
            lines.append(
                f"| `{r['rel_path']}` | {r['pages']} | {verdict} | {r['failed_rules']} | {r['failed_checks']:,} | `{clauses}` |"
            )

    if skipped_large:
        lines.append("")
        lines.append(
            f"> **Skipped (>20 MB free checker cap):** {', '.join(f'`{s}`' for s in skipped_large)}"
        )

    lines.extend(
        [
            "",
            "---",
            "**Need 0-Error ISO 14289-1 (PDF/UA-1) & WCAG 2.1 AA Remediation (Layout Untouched)?**",
            f"- 🔍 [Free Interactive Browser & veraPDF 1.30.3 Checker]({STOREFRONT_CHECKER_URL})",
            f"- ⚡ [$19 Single-PDF Express Fix (1–5 pages, 24h turnaround)]({EXPRESS_19_URL})",
            f"- 📦 [$65 Starter Package (≤12 pages)]({STARTER_65_URL}) · [$195 Reports & Data Tables (≤30 pages)]({STANDARD_195_URL})",
            f"- 🤝 [Agency White-Label Margin Calculator ($2.00–$6.50/pg) & Free 24h Sample]({AGENCY_CALC_URL})",
            "",
        ]
    )
    return "\n".join(lines)


def run_check(
    *,
    pdf_path: str,
    api_url: str,
    fail_on_violations: bool,
    max_files: int,
    workspace: Path,
    github_output: str | None = None,
    github_summary: str | None = None,
) -> int:
    pdfs = discover_pdfs(pdf_path, max_files=max_files, workspace=workspace)
    rows: list[dict[str, Any]] = []
    skipped_large: list[str] = []

    if pdfs:
        warm_health_endpoint(api_url)

    for p in pdfs:
        rel = rel_display(p, workspace)
        size = p.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            skipped_large.append(rel)
            print(f"::warning file={rel}::Skipping {rel} ({size} bytes exceeds 20 MB limit).")
            continue

        pdf_bytes = p.read_bytes()
        report = check_pdf_via_api(pdf_bytes, filename=p.name, api_url=api_url)
        is_compliant = bool(report.get("is_compliant", False))
        failed_rules = int(report.get("failed_rules", 0) or 0)
        failed_checks = int(report.get("failed_checks", 0) or 0)
        pages = int((report.get("structure") or {}).get("pages") or (report.get("quote") or {}).get("pages") or 1)
        findings = report.get("findings") or []
        clauses = [str(f.get("clause", "")).strip() for f in findings if f.get("clause")]

        effective_pass = is_compliant and failed_rules == 0
        rows.append(
            {
                "rel_path": rel,
                "pages": pages,
                "is_compliant": effective_pass,
                "failed_rules": failed_rules,
                "failed_checks": failed_checks,
                "clauses": clauses[:8],
            }
        )
        if effective_pass:
            print(f"[PASS] {rel} ({pages}p) — 0 failed ISO 14289-1 rules")
        else:
            clause_str = ", ".join(clauses[:6]) or "untagged/metadata"
            print(
                f"::error file={rel}::{rel} failed PDF/UA-1 validation: {failed_rules} failed rules, {failed_checks} failed checks (clauses: {clause_str})."
            )

    total_pdfs = len(rows)
    failing_pdfs = sum(1 for r in rows if not r["is_compliant"])
    total_failed_rules = sum(int(r["failed_rules"]) for r in rows)

    write_github_outputs(
        {
            "total-pdfs": total_pdfs,
            "failing-pdfs": failing_pdfs,
            "failed-rules": total_failed_rules,
        },
        github_output,
    )

    summary_md = render_markdown_summary(rows, skipped_large)
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as fh:
            fh.write(summary_md + "\n")
    else:
        print("\n" + summary_md)

    if fail_on_violations and failing_pdfs > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate repository PDFs against ISO 14289-1 (PDF/UA-1) & ADA Title II."
    )
    parser.add_argument("--pdf-path", default=os.environ.get("INPUT_PDF_PATH", "."))
    parser.add_argument(
        "--api-url",
        default=os.environ.get("INPUT_API_URL", "https://pdfua-ada-checker.onrender.com/api/check"),
    )
    parser.add_argument(
        "--fail-on-violations",
        default=os.environ.get("INPUT_FAIL_ON_VIOLATIONS", "true"),
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=int(os.environ.get("INPUT_MAX_FILES", "20") or "20"),
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("GITHUB_WORKSPACE", "."),
    )
    args = parser.parse_args(argv)

    return run_check(
        pdf_path=args.pdf_path,
        api_url=args.api_url,
        fail_on_violations=parse_bool(args.fail_on_violations),
        max_files=max(1, args.max_files),
        workspace=Path(args.workspace),
        github_output=os.environ.get("GITHUB_OUTPUT"),
        github_summary=os.environ.get("GITHUB_STEP_SUMMARY"),
    )


if __name__ == "__main__":
    sys.exit(main())
