from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_default_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
import urllib.parse
import xml.etree.ElementTree as ET

import pikepdf

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB strict cap
MAX_MULTIPART_OVERHEAD = 128 * 1024

STOREFRONT_URL = "https://hadesloc.github.io/pdfua-partner-desk/"
GOOGLE_FORM_URL = (
    "https://docs.google.com/forms/d/e/1FAIpQLSfC50VFJdc55ug-sNpJON59JL9By6yIuRe8qysotQLX1cNtFA/viewform"
)
UPWORK_CATALOG_URL = "https://www.upwork.com/services/product/2120536307204819908"
CHECKOUT_65_URL = "https://pdfuapartnerdesk.gumroad.com/l/pdfua-remediation-12-pages"
CHECKOUT_195_URL = "https://pdfuapartnerdesk.gumroad.com/l/pdfua-remediation-30-pages"
CONTACT_EMAIL = "halocrypto101@gmail.com"

CLAUSE_GUIDE: dict[str, dict[str, str]] = {
    "5": {
        "title": "Missing PDF/UA-1 Conformance Metadata (pdfuaid:part=1)",
        "wcag": "ISO 14289-1 §5 · Compliance Gateway Identification",
        "impact": "Automated procurement portals, PAC 2026, and municipal accessibility audits reject the file as uncertified because its XMP metadata stream lacks the PDF/UA-1 conformance identifier.",
        "severity": "HIGH",
    },
    "6.1": {
        "title": "XMP Metadata Stream Out of Sync with Document Info",
        "wcag": "ISO 14289-1 §6.1 · Metadata Consistency",
        "impact": "Assistive technologies read conflicting or corrupted document metadata streams.",
        "severity": "MEDIUM",
    },
    "6.2": {
        "title": "PDF Is Not Marked as Tagged (/MarkInfo /Marked false)",
        "wcag": "WCAG 2.1 SC 1.3.1 Info and Relationships (Level A)",
        "impact": "Screen readers like NVDA and JAWS treat the entire document as an unstructured wall of text or announce 'Alert: Empty document'.",
        "severity": "CRITICAL",
    },
    "7.1": {
        "title": "Untagged Page Content, Unmarked Artifacts, or Missing Document Title",
        "wcag": "WCAG 2.1 SC 1.3.1, 1.3.2 & 2.4.2 Page Titled (Level A)",
        "impact": "Blind users hear running page headers/footers injected mid-sentence, miss untagged text/graphics, or hear a cryptic filename instead of a human-readable document title.",
        "severity": "CRITICAL",
    },
    "7.2": {
        "title": "Missing Natural Language (/Lang) or Broken Structure Tree Hierarchy",
        "wcag": "WCAG 2.1 SC 3.1.1 Language of Page & 1.3.1 Structure (Level A)",
        "impact": "Text-to-speech synthesizers mispronounce every paragraph using the wrong phonetic engine, or skip malformed list/table tags (<LI>/<LBody>, <TR>/<TD>).",
        "severity": "HIGH",
    },
    "7.3": {
        "title": "Figures & Graphics Missing Alternative Text (/Alt)",
        "wcag": "WCAG 2.1 SC 1.1.1 Non-text Content (Level A)",
        "impact": "Charts, seals, maps, signatures, and diagrams are either completely invisible to blind readers or announced only as 'Graphic' with zero context.",
        "severity": "CRITICAL",
    },
    "7.4": {
        "title": "Skipped or Illogical Heading Hierarchy (<H1>–<H6>)",
        "wcag": "WCAG 2.1 SC 1.3.1 & 2.4.6 Headings and Labels (Level AA)",
        "impact": "Screen-reader users navigating by the H-key shortcut lose document outline orientation when headings skip levels (e.g., jumping from H1 straight to H4).",
        "severity": "HIGH",
    },
    "7.5": {
        "title": "Data Tables Missing Header Scopes (<TH Scope>) or Regular Grid Structure",
        "wcag": "WCAG 2.1 SC 1.3.1 Info and Relationships (Level A)",
        "impact": "Users reading municipal budgets, fee schedules, or academic tables hear raw numbers without knowing which row or column header each cell belongs to.",
        "severity": "CRITICAL",
    },
    "7.15": {
        "title": "Mathematical Formulas Missing Spoken Alternative Description",
        "wcag": "WCAG 2.1 SC 1.1.1 Non-text Content (Level A)",
        "impact": "Equations and technical symbols are skipped or read as garbled characters.",
        "severity": "HIGH",
    },
    "7.18.1": {
        "title": "Interactive Annotations Missing Accessible Labels (/Contents)",
        "wcag": "WCAG 2.1 SC 4.1.2 Name, Role, Value (Level A)",
        "impact": "Keyboard and screen-reader users focus interactive page annotations without hearing what they do.",
        "severity": "HIGH",
    },
    "7.18.3": {
        "title": "Keyboard Tab Order Not Bound to Logical Structure (/Tabs /S)",
        "wcag": "WCAG 2.1 SC 2.4.3 Focus Order (Level A)",
        "impact": "Pressing Tab jumps erratically across X/Y page coordinates instead of following the logical reading order of links and form fields.",
        "severity": "HIGH",
    },
    "7.18.4": {
        "title": "Fillable Form Fields Not Tagged Inside <Form> / Missing /TU Tooltips",
        "wcag": "WCAG 2.1 SC 1.3.1, 3.3.2 & 4.1.2 Form Accessibility (Level A/AA)",
        "impact": "Citizens or students using screen readers cannot tell what information to type into permit, HR, or enrollment form fields.",
        "severity": "CRITICAL",
    },
    "7.18.5": {
        "title": "Hyperlinks Missing Accessible Link Descriptions or <Link> /OBJR Binding",
        "wcag": "WCAG 2.1 SC 2.4.4 Link Purpose in Context (Level A)",
        "impact": "Screen readers spell out 120-character raw URLs character-by-character or skip clickable links completely.",
        "severity": "HIGH",
    },
    "7.21": {
        "title": "Unembedded Fonts, Missing Unicode Map (/ToUnicode), or Invalid CIDSet",
        "wcag": "ISO 14289-1 §7.21 · Font & Character Encoding Integrity",
        "impact": "PAC 2026 and veraPDF fail strict PDF/UA-1 certification; missing /ToUnicode maps cause screen readers to read gibberish symbols instead of words.",
        "severity": "MEDIUM",
    },
}


def _lookup_clause_info(clause: str) -> dict[str, str]:
    c = (clause or "").strip()
    if c in CLAUSE_GUIDE:
        return CLAUSE_GUIDE[c]
    parts = c.split(".")
    for length in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in CLAUSE_GUIDE:
            return CLAUSE_GUIDE[prefix]
    return {
        "title": f"ISO 14289-1 Clause {c or 'Structural'} Conformance Failure",
        "wcag": "ISO 14289-1 (PDF/UA-1) & WCAG 2.1 Level AA",
        "impact": "Fails strict PDF/UA-1 and PAC 2026 machine validation gates required by government and institutional accessibility standards.",
        "severity": "MEDIUM",
    }


def _discover_verapdf() -> tuple[str, Path] | None:
    env_jar = os.environ.get("VERAPDF_JAR", "").strip()
    jar_candidates: list[Path] = []
    if env_jar:
        jar_candidates.append(Path(env_jar))
    jar_candidates.append(Path("/opt/verapdf/cli-1.30.3.jar"))
    local_root = Path(__file__).resolve().parents[2]
    jar_candidates.append(local_root / "tools" / "verapdf" / "bin" / "cli-1.30.3.jar")

    jar_path = next((p for p in jar_candidates if p.is_file()), None)
    if jar_path is None:
        return None

    java_bin = shutil.which("java")
    if not java_bin:
        tools_dir = local_root / "tools"
        if tools_dir.is_dir():
            exe_name = "java.exe" if os.name == "nt" else "java"
            found = sorted(tools_dir.rglob(exe_name))
            if found:
                java_bin = str(found[0])
    if not java_bin:
        return None
    return java_bin, jar_path


def _run_verapdf(pdf_path: Path) -> dict[str, Any]:
    discovered = _discover_verapdf()
    if discovered is None:
        return {"available": False, "status": "UNAVAILABLE", "findings": []}
    java_bin, jar_path = discovered
    cmd = [
        java_bin,
        "-Xmx512m",
        "-Dfile.encoding=UTF8",
        "-XX:+IgnoreUnrecognizedVMOptions",
        "--add-exports=java.base/sun.security.pkcs=ALL-UNNAMED",
        "-cp",
        str(jar_path),
        "org.verapdf.apps.GreenfieldCliWrapper",
        "--flavour",
        "ua1",
        "--format",
        "mrr",
        str(pdf_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=50,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "status": "TIMEOUT", "findings": []}

    raw_xml = proc.stdout.decode("utf-8", errors="replace")
    idx = raw_xml.find("<report")
    if idx < 0:
        return {"available": True, "status": "ERROR", "findings": []}
    try:
        root = ET.fromstring(raw_xml[idx:])
    except ET.ParseError:
        return {"available": True, "status": "ERROR", "findings": []}

    val_rep = root.find(".//validationReport")
    if val_rep is None:
        return {"available": True, "status": "ENCRYPTED_OR_UNPARSEABLE", "findings": []}

    is_compliant = val_rep.attrib.get("isCompliant", "false").lower() == "true"
    details = val_rep.find("details")
    failed_rules = int(details.attrib.get("failedRules", "0")) if details is not None else 0
    passed_rules = int(details.attrib.get("passedRules", "0")) if details is not None else 0
    failed_checks = int(details.attrib.get("failedChecks", "0")) if details is not None else 0
    passed_checks = int(details.attrib.get("passedChecks", "0")) if details is not None else 0

    by_clause: dict[str, dict[str, Any]] = {}
    if details is not None:
        for rule in details.findall("rule"):
            if rule.attrib.get("status", "").lower() != "failed":
                continue
            clause = rule.attrib.get("clause", "").strip() or "Other"
            r_checks = int(rule.attrib.get("failedChecks", "1") or "1")
            desc_el = rule.find("description")
            raw_desc = " ".join((desc_el.text or "").split()) if desc_el is not None else ""
            meta = _lookup_clause_info(clause)
            bucket = by_clause.setdefault(
                clause,
                {
                    "clause": clause,
                    "title": meta["title"],
                    "wcag": meta["wcag"],
                    "impact": meta["impact"],
                    "severity": meta["severity"],
                    "failed_rules": 0,
                    "failed_checks": 0,
                    "spec_description": raw_desc,
                },
            )
            bucket["failed_rules"] += 1
            bucket["failed_checks"] += r_checks
            if raw_desc and not bucket["spec_description"]:
                bucket["spec_description"] = raw_desc

    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    findings = sorted(
        by_clause.values(),
        key=lambda x: (sev_rank.get(x["severity"], 3), -x["failed_checks"], x["clause"]),
    )
    return {
        "available": True,
        "status": "COMPLETED",
        "engine": "veraPDF 1.30.3 (ISO 14289-1:2014 PDF/UA-1)",
        "is_compliant": is_compliant and failed_rules == 0,
        "failed_rules": failed_rules,
        "passed_rules": passed_rules,
        "failed_checks": failed_checks,
        "passed_checks": passed_checks,
        "findings": findings,
    }


def _inspect_structure_and_quote(pdf_path: Path, verapdf_res: dict[str, Any]) -> dict[str, Any]:
    with pikepdf.Pdf.open(pdf_path) as pdf:
        pages = len(pdf.pages)
        root = pdf.Root
        mark_info = root.get("/MarkInfo")
        marked = bool(isinstance(mark_info, pikepdf.Dictionary) and bool(mark_info.get("/Marked", False)))
        sroot = root.get("/StructTreeRoot")
        has_struct_tree = isinstance(sroot, pikepdf.Dictionary)
        is_tagged = marked and has_struct_tree

        lang_val = str(root.get("/Lang", "")).strip() if "/Lang" in root else ""
        vp = root.get("/ViewerPreferences")
        display_doc_title = bool(
            isinstance(vp, pikepdf.Dictionary) and bool(vp.get("/DisplayDocTitle", False))
        )

        xmp_bytes = b""
        if "/Metadata" in root:
            try:
                xmp_bytes = bytes(root.Metadata.read_bytes())
            except Exception:
                xmp_bytes = b""
        xmp_text = xmp_bytes.decode("utf-8", errors="ignore")
        has_pdfua_id = "pdfuaid:part" in xmp_text
        has_xmp_title = ("dc:title" in xmp_text) and bool(re.search(r"<rdf:li[^>]*>[^<\s]", xmp_text))
        info_title = ""
        if pdf.docinfo and "/Title" in pdf.docinfo:
            try:
                info_title = str(pdf.docinfo["/Title"]).strip()
            except Exception:
                info_title = ""
        has_title = has_xmp_title or bool(info_title)

        acro = root.get("/AcroForm")
        form_fields_total = 0
        form_fields_missing_tu = 0
        is_signed = False
        if isinstance(acro, pikepdf.Dictionary) and "/Fields" in acro:
            seen_f: set[tuple[int, int]] = set()
            stack = list(acro.Fields)
            while stack:
                item = stack.pop()
                if not isinstance(item, pikepdf.Dictionary):
                    continue
                og = item.objgen
                if og != (0, 0):
                    if og in seen_f:
                        continue
                    seen_f.add(og)
                ft = str(item.get("/FT", ""))
                if ft == "/Sig" and "/V" in item:
                    is_signed = True
                if "/Kids" in item and "/T" not in item:
                    stack.extend(list(item.Kids))
                    continue
                if ft in ("/Tx", "/Btn", "/Ch") or ("/T" in item and ft != "/Sig"):
                    form_fields_total += 1
                    tu = str(item.get("/TU", "")).strip() if "/TU" in item else ""
                    if not tu:
                        form_fields_missing_tu += 1

        # Walk up to 2,500 structure nodes for fast table / figure / heading stats
        table_count = 0
        heading_count = 0
        figure_count = 0
        figures_missing_alt = 0
        if has_struct_tree:
            seen_s: set[tuple[int, int]] = set()
            s_stack: list[Any] = [sroot.get("/K")]
            visited = 0
            while s_stack and visited < 2500:
                curr = s_stack.pop()
                if isinstance(curr, pikepdf.Array):
                    s_stack.extend(list(curr))
                    continue
                if not isinstance(curr, pikepdf.Dictionary):
                    continue
                og = curr.objgen
                if og != (0, 0):
                    if og in seen_s:
                        continue
                    seen_s.add(og)
                visited += 1
                tag = str(curr.get("/S", "")).lstrip("/")
                if tag == "Table":
                    table_count += 1
                elif tag in ("H", "H1", "H2", "H3", "H4", "H5", "H6"):
                    heading_count += 1
                elif tag == "Figure":
                    figure_count += 1
                    alt = str(curr.get("/Alt", "")).strip() if "/Alt" in curr else ""
                    if not alt:
                        figures_missing_alt += 1
                if "/K" in curr:
                    s_stack.append(curr.get("/K"))

        # Check first up to 10 pages for scanned image-only heuristic & link annotations
        sample_pages = min(pages, 10)
        image_pages = 0
        textless_pages = 0
        links_missing_contents = 0
        for idx in range(sample_pages):
            pg = pdf.pages[idx]
            res = pg.get("/Resources")
            has_xobj_img = False
            if isinstance(res, pikepdf.Dictionary) and "/XObject" in res:
                xobj = res.get("/XObject")
                if isinstance(xobj, pikepdf.Dictionary):
                    for _, xo in xobj.items():
                        if isinstance(xo, pikepdf.Stream) and str(xo.get("/Subtype", "")) == "/Image":
                            has_xobj_img = True
                            break
            if has_xobj_img:
                image_pages += 1
            raw_stream = b""
            try:
                if "/Contents" in pg:
                    c_obj = pg.Contents
                    if isinstance(c_obj, pikepdf.Array):
                        for part in c_obj:
                            if isinstance(part, pikepdf.Stream):
                                raw_stream += bytes(part.read_bytes())[:8192]
                    elif isinstance(c_obj, pikepdf.Stream):
                        raw_stream = bytes(c_obj.read_bytes())[:16384]
            except Exception:
                raw_stream = b""
            if b"BT" not in raw_stream and b"Tj" not in raw_stream and b"TJ" not in raw_stream:
                textless_pages += 1

            if "/Annots" in pg and isinstance(pg.Annots, pikepdf.Array):
                for annot in pg.Annots:
                    if isinstance(annot, pikepdf.Dictionary) and str(annot.get("/Subtype", "")) == "/Link":
                        contents_val = str(annot.get("/Contents", "")).strip() if "/Contents" in annot else ""
                        if not contents_val:
                            links_missing_contents += 1

        likely_scanned_ocr = sample_pages > 0 and image_pages > 0 and textless_pages >= max(1, int(sample_pages * 0.7))

    has_forms = form_fields_total > 0
    has_tables = table_count > 0

    if has_forms:
        tier_code = "FILLABLE_ACROFORMS"
        tier_label = "Interactive Fillable AcroForms"
        wholesale_per_page = 6.50
        retail_per_page = 15.00
    elif likely_scanned_ocr:
        tier_code = "SCANNED_OCR"
        tier_label = "Scanned OCR + Searchable PDF/UA"
        wholesale_per_page = 3.50
        retail_per_page = 9.00
    elif has_tables:
        tier_code = "STRUCTURED_TABLES"
        tier_label = "Structured Data Tables & Reports"
        wholesale_per_page = 3.00
        retail_per_page = 8.00
    else:
        tier_code = "SIMPLE_TEXT"
        tier_label = "Born-Digital Text & Headings"
        wholesale_per_page = 2.00
        retail_per_page = 5.00

    raw_wholesale = round(pages * wholesale_per_page, 2)
    wholesale_total = max(35.00, raw_wholesale) if pages > 0 else 35.00
    retail_total = max(65.00, round(pages * retail_per_page, 2))
    agency_profit = round(max(0.0, retail_total - wholesale_total), 2)

    if not has_forms and not likely_scanned_ocr and not has_tables and pages <= 12:
        fixed_pkg_label = "Starter PDF/UA Package ($65 up to 12 pages)"
        fixed_pkg_usd = 65.00
    elif not has_forms and not likely_scanned_ocr and pages <= 30:
        fixed_pkg_label = "Standard ADA Title II Reports & Tables ($195 up to 30 pages)"
        fixed_pkg_usd = 195.00
    elif (likely_scanned_ocr and pages <= 35) or (has_forms and pages <= 25):
        fixed_pkg_label = "Complex OCR / Fillable AcroForm Package ($390)"
        fixed_pkg_usd = 390.00
    else:
        fixed_pkg_label = f"Custom Batch Rate (${wholesale_total:,.2f} Agency Wholesale)"
        fixed_pkg_usd = wholesale_total

    # Fallback plain-English findings if veraPDF was unavailable
    findings = list(verapdf_res.get("findings") or [])
    if not verapdf_res.get("available"):
        if not is_tagged:
            m = _lookup_clause_info("6.2")
            findings.append({"clause": "6.2", **m, "failed_rules": 1, "failed_checks": pages})
        if not has_pdfua_id:
            m = _lookup_clause_info("5")
            findings.append({"clause": "5", **m, "failed_rules": 1, "failed_checks": 1})
        if not has_title or not display_doc_title:
            m = _lookup_clause_info("7.1")
            findings.append({"clause": "7.1", **m, "failed_rules": 1, "failed_checks": 1})
        if not lang_val:
            m = _lookup_clause_info("7.2")
            findings.append({"clause": "7.2", **m, "failed_rules": 1, "failed_checks": 1})
        if figures_missing_alt > 0:
            m = _lookup_clause_info("7.3")
            findings.append({"clause": "7.3", **m, "failed_rules": 1, "failed_checks": figures_missing_alt})
        if form_fields_missing_tu > 0:
            m = _lookup_clause_info("7.18.4")
            findings.append({"clause": "7.18.4", **m, "failed_rules": 1, "failed_checks": form_fields_missing_tu})
        if links_missing_contents > 0:
            m = _lookup_clause_info("7.18.5")
            findings.append({"clause": "7.18.5", **m, "failed_rules": 1, "failed_checks": links_missing_contents})

    is_pass = (
        bool(verapdf_res.get("is_compliant"))
        if verapdf_res.get("status") == "COMPLETED"
        else (len(findings) == 0 and is_tagged and has_pdfua_id)
    )

    return {
        "structure": {
            "pages": pages,
            "is_tagged": is_tagged,
            "has_pdfua_id": has_pdfua_id,
            "language": lang_val or None,
            "has_title": has_title,
            "display_doc_title": display_doc_title,
            "table_count": table_count,
            "heading_count": heading_count,
            "figure_count": figure_count,
            "figures_missing_alt": figures_missing_alt,
            "form_fields_total": form_fields_total,
            "form_fields_missing_tu": form_fields_missing_tu,
            "links_missing_contents_sample": links_missing_contents,
            "likely_scanned_ocr": likely_scanned_ocr,
            "is_cryptographically_signed": is_signed,
        },
        "quote": {
            "tier_code": tier_code,
            "tier_label": tier_label,
            "pages": pages,
            "wholesale_per_page_usd": wholesale_per_page,
            "wholesale_total_usd": wholesale_total,
            "retail_per_page_usd": retail_per_page,
            "retail_total_usd": retail_total,
            "agency_net_margin_usd": agency_profit,
            "fixed_package_label": fixed_pkg_label,
            "fixed_package_usd": fixed_pkg_usd,
            "free_sample_eligible": pages <= 15,
        },
        "is_compliant": is_pass,
        "findings": findings,
    }


def analyze_pdf_bytes(pdf_bytes: bytes, filename: str = "document.pdf") -> dict[str, Any]:
    started = time.monotonic()
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 20 MB maximum upload limit.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Uploaded file is not a valid PDF (%PDF- header missing).")

    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name).strip() or "document.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"

    tmp_dir = Path(tempfile.mkdtemp(prefix="pdfua-ephemeral-"))
    tmp_pdf = tmp_dir / "input.pdf"
    ephemeral_deleted = False
    try:
        tmp_pdf.write_bytes(pdf_bytes)
        verapdf_res = _run_verapdf(tmp_pdf)
        enriched = _inspect_structure_and_quote(tmp_pdf, verapdf_res)
    except pikepdf.PasswordError as exc:
        raise ValueError("Password-protected PDF cannot be inspected without unlocking.") from exc
    except pikepdf.PdfError as exc:
        raise ValueError(f"Malformed or corrupted PDF file: {exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        ephemeral_deleted = not tmp_dir.exists()

    failed_rules = (
        verapdf_res.get("failed_rules", len(enriched["findings"]))
        if verapdf_res.get("status") == "COMPLETED"
        else len(enriched["findings"])
    )
    failed_checks = (
        verapdf_res.get("failed_checks", sum(f.get("failed_checks", 1) for f in enriched["findings"]))
        if verapdf_res.get("status") == "COMPLETED"
        else sum(f.get("failed_checks", 1) for f in enriched["findings"])
    )

    quote = enriched["quote"]
    pages = quote["pages"]
    clauses_list = ", ".join(f["clause"] for f in enriched["findings"][:6]) or "None"
    mail_subject = (
        f"Fix PDF/UA-1 Issues: {safe_name} ({pages} pages · {failed_rules} failed rules)"
    )
    mail_body = (
        f"Hi Loc,\n\n"
        f"I just ran \"{safe_name}\" ({pages} pages) through the Free PDF/UA & ADA Title II Checker:\n"
        f"- Status: {'PASS (0 failed rules)' if enriched['is_compliant'] else f'FAIL ({failed_rules} failed rules / {failed_checks} failed checks)'}\n"
        f"- Detected Complexity Tier: {quote['tier_label']}\n"
        f"- Violated ISO 14289-1 Clauses: {clauses_list}\n"
        f"- Quoted Partner Wholesale Cost: ${quote['wholesale_total_usd']:,.2f} (or Fixed Package: ${quote['fixed_package_usd']:,.2f})\n\n"
        f"Please let me know the turnaround to remediate this document (or run a free sample proof):\n"
    )
    mailto_url = (
        f"mailto:{urllib.parse.quote(CONTACT_EMAIL)}"
        f"?subject={urllib.parse.quote(mail_subject)}"
        f"&body={urllib.parse.quote(mail_body)}"
    )

    return {
        "filename": safe_name,
        "file_size_bytes": len(pdf_bytes),
        "verdict": "PASS_PDFUA1" if enriched["is_compliant"] else "ACTION_REQUIRED",
        "is_compliant": enriched["is_compliant"],
        "engine": verapdf_res.get("engine", "Structural PDF/UA-1 Inspector"),
        "verapdf_status": verapdf_res.get("status", "UNAVAILABLE"),
        "failed_rules": failed_rules,
        "passed_rules": verapdf_res.get("passed_rules", 0),
        "failed_checks": failed_checks,
        "passed_checks": verapdf_res.get("passed_checks", 0),
        "structure": enriched["structure"],
        "findings": enriched["findings"],
        "quote": quote,
        "cta": {
            "checkout_65_url": CHECKOUT_65_URL,
            "checkout_195_url": CHECKOUT_195_URL,
            "direct_checkout_url": (
                CHECKOUT_65_URL
                if quote["fixed_package_usd"] <= 65
                else (CHECKOUT_195_URL if quote["fixed_package_usd"] <= 195 else UPWORK_CATALOG_URL)
            ),
            "upwork_catalog_url": UPWORK_CATALOG_URL,
            "google_form_url": GOOGLE_FORM_URL,
            "storefront_url": STOREFRONT_URL,
            "mailto_url": mailto_url,
            "contact_email": CONTACT_EMAIL,
        },
        "privacy": {
            "max_upload_mb": 20,
            "ephemeral_deleted": ephemeral_deleted,
            "files_retained": 0,
        },
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def _extract_upload(headers: Any, body: bytes) -> tuple[bytes, str]:
    ctype = (headers.get("Content-Type") or "").strip()
    if ctype.lower().startswith("application/pdf") or body.startswith(b"%PDF-"):
        fname = urllib.parse.unquote(headers.get("X-Filename") or "uploaded.pdf")
        return body, fname

    if "multipart/form-data" in ctype.lower():
        raw_msg = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        msg = BytesParser(policy=email_default_policy).parsebytes(raw_msg)
        for part in msg.iter_parts():
            fname = part.get_filename()
            payload = part.get_payload(decode=True)
            if payload and (fname or payload.startswith(b"%PDF-")):
                return payload, fname or "uploaded.pdf"
    raise ValueError("No PDF file found in upload request.")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free Online PDF/UA-1, ADA Title II &amp; Section 508 Checker (veraPDF 1.30.3)</title>
<meta name="description" content="Instant free PDF accessibility validator powered by veraPDF 1.30.3 (ISO 14289-1 PDF/UA-1). Check ADA Title II, WCAG 2.1 AA, and Section 508 issues in plain English with zero file retention.">
<style>
  :root {
    --bg: #ffffff;
    --bg-subtle: #f8fafc;
    --bg-muted: #f1f5f9;
    --line: #e2e8f0;
    --line-strong: #cbd5e1;
    --ink: #0f172a;
    --ink-2: #334155;
    --muted: #475569;
    --brand: #1d4ed8;
    --brand-hover: #1e40af;
    --brand-soft: #eff6ff;
    --ok: #166534;
    --ok-bg: #f0fdf4;
    --ok-border: #bbf7d0;
    --err: #991b1b;
    --err-bg: #fef2f2;
    --err-border: #fecaca;
    --warn: #92400e;
    --warn-bg: #fffbeb;
    --warn-border: #fde68a;
    --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.05);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--brand); text-decoration: underline; text-underline-offset: 3px; }
  a:hover { color: var(--brand-hover); }
  a:focus-visible, button:focus-visible, [role="button"]:focus-visible {
    outline: 3px solid var(--brand);
    outline-offset: 2px;
  }
  .skip-link {
    position: absolute;
    left: -9999px;
    top: 8px;
    z-index: 9999;
    background: var(--ink);
    color: #ffffff;
    font-weight: 700;
    padding: 10px 16px;
    border-radius: 6px;
    text-decoration: none;
  }
  .skip-link:focus { left: 12px; }
  .site-header {
    background: var(--bg);
    border-bottom: 1px solid var(--line);
    position: sticky;
    top: 0;
    z-index: 40;
  }
  .header-inner {
    max-width: 1080px;
    margin: 0 auto;
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-weight: 650;
    font-size: 1rem;
    color: var(--ink);
    text-decoration: none;
  }
  .brand:hover { color: var(--ink); text-decoration: none; }
  .brand-mark {
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--brand);
    color: #ffffff;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .header-actions {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
  }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 56px; }
  .eyebrow {
    display: inline-block;
    font-size: 0.84rem;
    font-weight: 600;
    color: var(--brand);
    margin-bottom: 8px;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }
  .pill-ok { background: var(--ok-bg); color: var(--ok); border: 1px solid var(--ok-border); }
  .pill-warn { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-border); }
  .pill-err { background: var(--err-bg); color: var(--err); border: 1px solid var(--err-border); }
  .pill-brand { background: var(--brand-soft); color: #1e3a8a; border: 1px solid #bfdbfe; }
  h1 {
    font-size: clamp(1.85rem, 3.2vw, 2.5rem);
    margin: 4px 0 12px;
    line-height: 1.15;
    letter-spacing: -0.025em;
    color: var(--ink);
  }
  .sublead { color: var(--ink-2); max-width: 740px; margin: 0 0 28px; font-size: 1.05rem; }
  .dropzone {
    background: var(--bg-subtle);
    border: 1.5px dashed #94a3b8;
    border-radius: 10px;
    padding: 36px 24px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s ease, background-color 0.15s ease;
  }
  .dropzone:hover { border-color: var(--brand); background: var(--brand-soft); }
  .dropzone.dragover { border-color: var(--ok); background: var(--ok-bg); }
  .chk-icon {
    width: 44px;
    height: 44px;
    margin: 0 auto 12px;
    border-radius: 8px;
    background: var(--brand-soft);
    color: var(--brand);
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .dropzone h2 { margin: 4px 0 6px; font-size: 1.15rem; color: var(--ink); letter-spacing: -0.015em; }
  .dropzone p { margin: 0 auto 18px; max-width: 580px; color: var(--muted); font-size: 0.9rem; }
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-height: 40px;
    padding: 10px 18px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 0.9rem;
    border: 1px solid transparent;
    cursor: pointer;
    font-family: inherit;
    text-decoration: none;
    transition: background-color 0.15s ease, border-color 0.15s ease;
  }
  .btn:hover { text-decoration: none; }
  .btn-primary { background: var(--brand); border-color: var(--brand); color: #ffffff; }
  .btn-primary:hover { background: var(--brand-hover); border-color: var(--brand-hover); color: #ffffff; }
  .btn-sec { background: #ffffff; color: var(--ink); border-color: var(--line-strong); }
  .btn-sec:hover { background: var(--bg-subtle); border-color: #94a3b8; color: var(--ink); }
  .btn-sm { min-height: 36px; padding: 7px 13px; font-size: 0.82rem; }
  .privacy-strip {
    display: flex;
    gap: 20px;
    justify-content: center;
    flex-wrap: wrap;
    margin-top: 14px;
    font-size: 0.82rem;
    color: var(--muted);
  }
  .privacy-strip span { display: inline-flex; align-items: center; gap: 6px; }
  .privacy-strip span::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--brand);
  }
  .card {
    background: #ffffff;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 24px;
    margin-top: 24px;
    box-shadow: var(--shadow-sm);
  }
  .result-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-top: 18px; }
  .kpi { background: var(--bg-subtle); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
  .kpi .lbl { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); font-weight: 600; }
  .kpi .val { font-size: 1.55rem; font-weight: 800; margin: 4px 0 2px; color: var(--ink); font-variant-numeric: tabular-nums; }
  .kpi .val.ok { color: var(--ok); }
  .kpi .val.err { color: var(--err); }
  .kpi .val.brand { color: var(--brand); }
  .kpi .hint { font-size: 0.82rem; color: var(--ink-2); }
  .cta-banner {
    background: var(--bg-subtle);
    border: 1px solid var(--line);
    border-left: 4px solid var(--brand);
    border-radius: 10px;
    padding: 24px;
    margin-top: 24px;
  }
  .cta-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
  .mor-note { margin: 10px 0 0; font-size: 0.78rem; color: var(--muted); }
  .issue-item {
    background: var(--bg-subtle);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 16px 18px;
    margin-top: 12px;
  }
  .issue-item.issue-pass {
    background: var(--ok-bg);
    border-color: var(--ok-border);
  }
  .issue-head { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; align-items: center; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82rem; color: var(--muted); }
  .checks-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin-top: 16px; }
  .chk {
    padding: 10px 12px;
    border-radius: 6px;
    background: #ffffff;
    border: 1px solid var(--line);
    font-size: 0.85rem;
    color: var(--ink-2);
  }
  .chk strong.ok { color: var(--ok); }
  .chk strong.err { color: var(--err); }
  #status-box {
    display: none;
    margin-top: 18px;
    padding: 14px 18px;
    border-radius: 8px;
    background: var(--brand-soft);
    border: 1px solid #bfdbfe;
    text-align: center;
    font-weight: 600;
    color: #1e3a8a;
  }
  #error-box {
    display: none;
    margin-top: 18px;
    background: var(--err-bg);
    border: 1px solid var(--err-border);
    color: var(--err);
    padding: 14px 18px;
    border-radius: 8px;
  }
  #results { display: none; }
  footer {
    margin-top: 48px;
    padding: 28px 0 12px;
    border-top: 1px solid var(--line);
    font-size: 0.84rem;
    color: var(--muted);
  }
  footer a { color: var(--ink-2); font-weight: 600; }
  @media (max-width: 640px) {
    .header-inner { padding: 10px 16px; }
    .wrap { padding: 28px 16px 40px; }
    .card, .cta-banner { padding: 18px; }
    .cta-row .btn { width: 100%; }
  }
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<header class="site-header">
  <div class="header-inner">
    <a class="brand" href="https://hadesloc.github.io/pdfua-partner-desk/">
      <span class="brand-mark" aria-hidden="true">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><path d="m9 15 2 2 4-4"></path></svg>
      </span>
      <span>PDF/UA Partner Desk</span>
    </a>
    <div class="header-actions">
      <span class="pill pill-brand">veraPDF 1.30.3 Engine</span>
      <a class="btn btn-sec btn-sm" href="https://hadesloc.github.io/pdfua-partner-desk/#calculator">Wholesale Estimator</a>
      <a class="btn btn-primary btn-sm" href="https://hadesloc.github.io/pdfua-partner-desk/#free-sample">Free 24h Sample</a>
    </div>
  </div>
</header>

<main id="main-content" tabindex="-1" class="wrap">
  <span class="eyebrow">ISO 14289-1:2014 (PDF/UA-1) &middot; ADA Title II &middot; Section 508</span>
  <h1>Free Online PDF/UA-1 &amp; WCAG 2.1 AA Compliance Checker</h1>
  <p class="sublead">
    Validate any municipal, university, healthcare, or agency PDF against real <strong>veraPDF 1.30.3</strong> rules. Review plain-English clause diagnostics and turnkey remediation pricing with zero file retention.
  </p>

  <input type="file" id="file-input" accept="application/pdf,.pdf" aria-label="Select PDF file up to 20 megabytes" tabindex="-1" style="display:none">
  <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Upload PDF file up to 20 megabytes">
    <div class="chk-icon" aria-hidden="true">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
    </div>
    <h2>Drop a PDF here, or click to run an instant PDF/UA-1 audit</h2>
    <p>Evaluates ISO 14289-1 tags, reading order, figure alt text, table header scopes, AcroForm /TU tooltips, font encoding, and scanned text layers.</p>
    <span class="btn btn-primary">Select PDF to Validate (Max 20 MB)</span>
  </div>
  <div class="privacy-strip">
    <span>Zero file retention &mdash; deleted immediately after scan</span>
    <span>Real veraPDF 1.30.3 Java engine</span>
    <span>Free &middot; No sign-up required</span>
  </div>

  <div id="status-box" role="status" aria-live="polite">Running veraPDF 1.30.3 ISO 14289-1 (PDF/UA-1) &amp; structural inspection&hellip;</div>
  <div id="error-box" role="alert"></div>

  <div id="results">
    <div class="card">
      <div class="result-head">
        <div>
          <span id="verdict-pill" class="pill pill-err">NON-COMPLIANT</span>
          <h2 id="doc-title" style="margin:8px 0 2px; font-size:1.35rem;">document.pdf</h2>
          <div id="engine-line" class="mono"></div>
        </div>
        <button type="button" class="btn btn-sec btn-sm" id="btn-download-report">Download HTML Audit Report</button>
      </div>

      <div class="kpi-grid">
        <div class="kpi">
          <div class="lbl">PDF/UA-1 Failed Rules</div>
          <div class="val" id="kpi-rules">0</div>
          <div class="hint" id="kpi-checks">0 failed clause checks</div>
        </div>
        <div class="kpi">
          <div class="lbl">Document Scope</div>
          <div class="val" id="kpi-pages">0 Pages</div>
          <div class="hint" id="kpi-tier">Born-Digital Text</div>
        </div>
        <div class="kpi">
          <div class="lbl">Turnkey Package</div>
          <div class="val brand" id="kpi-quote">$65</div>
          <div class="hint" id="kpi-wholesale">Agency Wholesale: $35</div>
        </div>
        <div class="kpi">
          <div class="lbl">Privacy Verification</div>
          <div class="val ok" style="font-size:1.3rem;">0 Retained</div>
          <div class="hint">Ephemeral file purged after scan</div>
        </div>
      </div>

      <div class="checks-grid" id="struct-grid"></div>
    </div>

    <div class="cta-banner" id="cta-banner">
      <span class="pill pill-ok">Pixel-Identical Layout &middot; 0 Failed veraPDF 1.30.3 Rules</span>
      <h2 id="cta-headline" style="margin:10px 0 6px; font-size:1.3rem;">Need this PDF remediated to 0 failed PDF/UA-1 rules?</h2>
      <p id="cta-sub" style="margin:0; color:var(--ink-2); font-size:0.95rem;"></p>
      <div class="cta-row">
        <a id="cta-checkout" class="btn btn-primary" href="#" target="_blank" rel="noopener">Order Turnkey Package ($65)</a>
        <a id="cta-upwork" class="btn btn-sec" href="#" target="_blank" rel="noopener">Order via Upwork Escrow</a>
        <a id="cta-form" class="btn btn-sec" href="#" target="_blank" rel="noopener">Request Free 24h Sample</a>
        <a id="cta-mailto" class="btn btn-sec" href="#">Email Quote Breakdown</a>
      </div>
      <p class="mor-note">Secure checkout by Gumroad</p>
    </div>

    <div class="card">
      <h2 style="margin-top:0; font-size:1.25rem;">Plain-English ISO 14289-1 &amp; WCAG 2.1 AA Diagnostic Findings</h2>
      <div id="issues-list"></div>
    </div>
  </div>

  <footer>
    <p>
      <strong>Privacy &amp; Technical Scope Notice:</strong> Uploaded files (max 20 MB) are processed inside an isolated ephemeral directory and permanently deleted before this page receives your report. Passing automated ISO 14289-1 (PDF/UA-1) rules in veraPDF 1.30.3 verifies machine structure, tags, metadata, and fonts; full WCAG 2.1 AA compliance also requires human review of alt-text accuracy and logical reading order in NVDA. Technical compliance engineering service &mdash; not legal advice.
    </p>
    <p>
      Operated by <strong>Loc Nguyen &mdash; PDF/UA Partner Desk</strong> (Vietnam) &middot;
      <a href="https://hadesloc.github.io/pdfua-partner-desk/">Storefront &amp; Wholesale Desk</a> &middot;
      <a href="mailto:halocrypto101@gmail.com">halocrypto101@gmail.com</a>
    </p>
  </footer>
</main>

<script>
(function () {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const statusBox = document.getElementById("status-box");
  const errorBox = document.getElementById("error-box");
  const resultsBox = document.getElementById("results");
  let lastReport = null;

  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const fmtUsd = (n) => "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
      runCheck(e.dataTransfer.files[0]);
    }
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) runCheck(fileInput.files[0]);
  });

  async function runCheck(file) {
    errorBox.style.display = "none";
    resultsBox.style.display = "none";
    if (file.size > 20 * 1024 * 1024) {
      errorBox.innerHTML = "<strong>File exceeds 20 MB limit.</strong> For large 50&ndash;500 page reports, <a href='https://docs.google.com/forms/d/e/1FAIpQLSfC50VFJdc55ug-sNpJON59JL9By6yIuRe8qysotQLX1cNtFA/viewform' target='_blank' rel='noopener'>submit your PDF link for a free workstation audit &rarr;</a>";
      errorBox.style.display = "block";
      return;
    }
    statusBox.style.display = "block";
    try {
      const resp = await fetch("./api/check", {
        method: "POST",
        headers: {
          "Content-Type": "application/pdf",
          "X-Filename": encodeURIComponent(file.name || "document.pdf")
        },
        body: file
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || ("Validation error (" + resp.status + ")"));
      }
      lastReport = data;
      renderReport(data);
    } catch (err) {
      errorBox.textContent = "Error: " + (err && err.message ? err.message : String(err));
      errorBox.style.display = "block";
    } finally {
      statusBox.style.display = "none";
    }
  }

  function renderReport(d) {
    const pill = document.getElementById("verdict-pill");
    if (d.is_compliant) {
      pill.className = "pill pill-ok";
      pill.textContent = "PASS · 0 FAILED PDF/UA-1 RULES";
    } else {
      pill.className = "pill pill-err";
      pill.textContent = "NON-COMPLIANT · FAILS PDF/UA-1";
    }
    document.getElementById("doc-title").textContent = d.filename + " (" + (d.file_size_bytes / 1024).toFixed(1) + " KB)";
    document.getElementById("engine-line").textContent = d.engine + " · Completed in " + d.elapsed_seconds + "s";

    const rulesEl = document.getElementById("kpi-rules");
    rulesEl.textContent = d.failed_rules;
    rulesEl.className = "val " + (d.is_compliant ? "ok" : "err");
    document.getElementById("kpi-checks").textContent = Number(d.failed_checks || 0).toLocaleString("en-US") + " failed clause checks";

    const q = d.quote || {};
    const st = d.structure || {};
    document.getElementById("kpi-pages").textContent = q.pages + (q.pages === 1 ? " Page" : " Pages");
    document.getElementById("kpi-tier").textContent = q.tier_label || "";
    document.getElementById("kpi-quote").textContent = fmtUsd(q.fixed_package_usd);
    document.getElementById("kpi-wholesale").textContent = "Agency Wholesale: " + fmtUsd(q.wholesale_total_usd) + " (Typical retail: " + fmtUsd(q.retail_total_usd) + ")";

    const checks = [
      [st.is_tagged, "Tagged Structure (/StructTreeRoot)"],
      [st.has_pdfua_id, "PDF/UA-1 XMP ID (pdfuaid:part=1)"],
      [Boolean(st.language), "Document Language (" + (st.language || "Missing") + ")"],
      [st.has_title && st.display_doc_title, "Accessible Title & DisplayDocTitle"],
      [st.figures_missing_alt === 0, "Figures Alt-Text (" + st.figures_missing_alt + " missing /Alt)"],
      [st.form_fields_missing_tu === 0, "Form Tooltips (" + st.form_fields_total + " fields, " + st.form_fields_missing_tu + " missing /TU)"],
      [!st.likely_scanned_ocr, st.likely_scanned_ocr ? "Scanned Image PDF (Needs OCR)" : "Searchable Text Layer Present"],
      [!st.is_cryptographically_signed, st.is_cryptographically_signed ? "Cryptographic Signature Lock Detected" : "Ready for Lossless Remediation"]
    ];
    document.getElementById("struct-grid").innerHTML = checks.map(([ok, label]) =>
      "<div class='chk'><strong class='" + (ok ? "ok" : "err") + "'>" + (ok ? "Pass:" : "Issue:") + "</strong> " + esc(label) + "</div>"
    ).join("");

    document.getElementById("cta-headline").textContent = d.is_compliant
      ? "Automated Rules Passed — Need Human NVDA Reading-Order Verification or Backlog Pricing?"
      : "Remediate \"" + d.filename + "\" (" + q.pages + "p) to 0 Failed Rules — " + fmtUsd(q.wholesale_total_usd) + " Wholesale or " + fmtUsd(q.fixed_package_usd) + " Direct";
    document.getElementById("cta-sub").textContent =
      "Delivered with pixel-identical visual layout, semantic headings/tables/alt-text, veraPDF 1.30.3 validation report (0 failed rules), NVDA reading-order QA, and SHA-256 manifest. Agencies save " +
      fmtUsd(q.agency_net_margin_usd) + " vs. typical US retail (" + fmtUsd(q.retail_total_usd) + ") — or request a complimentary 24-hour sample proof.";

    document.getElementById("cta-checkout").href = d.cta.direct_checkout_url || d.cta.upwork_catalog_url;
    document.getElementById("cta-checkout").textContent = "Order Turnkey Package (" + fmtUsd(q.fixed_package_usd) + ")";
    document.getElementById("cta-upwork").href = d.cta.upwork_catalog_url;
    document.getElementById("cta-upwork").textContent = "Order via Upwork Escrow";
    document.getElementById("cta-form").href = d.cta.google_form_url;
    document.getElementById("cta-mailto").href = d.cta.mailto_url;

    const listEl = document.getElementById("issues-list");
    if (!d.findings || d.findings.length === 0) {
      listEl.innerHTML = "<div class='issue-item issue-pass'><strong style='color:var(--ok);'>Zero ISO 14289-1 (PDF/UA-1) Rule Failures Detected</strong><p style='margin:6px 0 0; color:var(--ink-2);'>This PDF passed all automated veraPDF 1.30.3 PDF/UA-1 clauses. Verify semantic alt-text accuracy and logical reading order in NVDA before publishing.</p></div>";
    } else {
      listEl.innerHTML = d.findings.map((f) => {
        const pCls = f.severity === "CRITICAL" ? "pill-err" : "pill-warn";
        return "<div class='issue-item'>" +
          "<div class='issue-head'>" +
            "<strong>Clause " + esc(f.clause) + " — " + esc(f.title) + "</strong>" +
            "<span class='pill " + pCls + "'>" + esc(f.severity) + " · " + Number(f.failed_checks).toLocaleString("en-US") + " checks</span>" +
          "</div>" +
          "<div class='mono' style='margin-top:4px;'>" + esc(f.wcag) + "</div>" +
          "<p style='margin:8px 0 0; color:var(--ink-2); font-size:0.93rem;'><strong>User &amp; ADA Impact:</strong> " + esc(f.impact) + "</p>" +
        "</div>";
      }).join("");
    }
    resultsBox.style.display = "block";
    resultsBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  document.getElementById("btn-download-report").addEventListener("click", () => {
    if (!lastReport) return;
    const d = lastReport;
    const rows = (d.findings || []).map((f) =>
      "<tr><td><strong>ISO 14289-1 §" + esc(f.clause) + "</strong></td><td><strong>" + esc(f.title) + "</strong><br><small>" + esc(f.wcag) + "</small></td><td>" + esc(f.impact) + "</td><td>" + f.failed_checks + "</td></tr>"
    ).join("") || "<tr><td colspan='4'>0 failed rules detected in veraPDF 1.30.3.</td></tr>";
    const htmlDoc = "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>PDF/UA-1 Audit Report — " + esc(d.filename) + "</title>" +
      "<style>body{font-family:sans-serif;max-width:960px;margin:32px auto;padding:0 20px;color:#0f172a;line-height:1.5}table{width:100%;border-collapse:collapse;margin:18px 0}th,td{border:1px solid #cbd5e1;padding:10px;text-align:left;vertical-align:top}th{background:#f1f5f9}.box{background:#eff6ff;border:1px solid #bfdbfe;border-left:4px solid #1d4ed8;border-radius:8px;padding:18px;margin:20px 0}</style></head><body>" +
      "<h1>ADA Title II &amp; ISO 14289-1 (PDF/UA-1) Audit Report</h1>" +
      "<p><strong>File:</strong> " + esc(d.filename) + " (" + d.quote.pages + " pages) &middot; <strong>Verdict:</strong> " + (d.is_compliant ? "PASS (0 Failed Rules)" : "NON-COMPLIANT (" + d.failed_rules + " Failed Rules / " + d.failed_checks + " Failed Checks)") + "</p>" +
      "<div class='box'><strong>Turnkey Remediation Quote (" + esc(d.quote.tier_label) + "):</strong> " + fmtUsd(d.quote.wholesale_total_usd) + " White-Label Agency Wholesale (or " + fmtUsd(d.quote.fixed_package_usd) + " Fixed Package).<br>" +
      "Request Free 24h Sample: <a href='" + esc(d.cta.google_form_url) + "'>" + esc(d.cta.google_form_url) + "</a> &middot; Upwork Escrow: <a href='" + esc(d.cta.upwork_catalog_url) + "'>" + esc(d.cta.upwork_catalog_url) + "</a> &middot; Direct: halocrypto101@gmail.com</div>" +
      "<table><thead><tr><th scope='col'>Clause</th><th scope='col'>Issue &amp; WCAG Criterion</th><th scope='col'>Assistive Tech Impact</th><th scope='col'>Failed Checks</th></tr></thead><tbody>" + rows + "</tbody></table>" +
      "</body></html>";
    const blob = new Blob([htmlDoc], { type: "text/html;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = d.filename.replace(/\.pdf$/i, "") + "-pdfua-audit-report.html";
    a.click();
  });
})();
</script>
</body>
</html>
"""


class CheckerHTTPHandler(BaseHTTPRequestHandler):
    server_version = "PDFUAChecker/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            discovered = _discover_verapdf()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "verapdf_ready": discovered is not None,
                    "max_upload_mb": 20,
                    "files_retained": 0,
                },
            )
            return
        if path in ("/", "/index.html"):
            raw = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self._send_cors()
            self.end_headers()
            self.wfile.write(raw)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/check":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            clen = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            clen = 0
        if clen <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Empty request body."})
            return
        if clen > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "File exceeds the 20 MB free checker limit."},
            )
            return

        body = self.rfile.read(clen)
        try:
            pdf_bytes, filename = _extract_upload(self.headers, body)
            if len(pdf_bytes) > MAX_UPLOAD_BYTES:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "File exceeds the 20 MB free checker limit."},
                )
                return
            report = analyze_pdf_bytes(pdf_bytes, filename=filename)
            self._send_json(HTTPStatus.OK, report)
        except ValueError as exc:
            status = (
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                if "20 MB" in str(exc)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json(status, {"error": str(exc)})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Checker error: {exc}"},
            )


def main() -> None:
    port = int(os.environ.get("PORT", "7860"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), CheckerHTTPHandler)
    print(f"PDF/UA & ADA Title II Checker listening on 0.0.0.0:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
