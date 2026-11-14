from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

import action_runner


class _MockCheckerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/api/health":
            body = json.dumps({"ok": True, "verapdf_ready": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        clen = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(clen)
        fname = self.headers.get("X-Filename") or "doc.pdf"
        is_clean = b"CLEAN_SPECIMEN" in raw
        payload = {
            "filename": fname,
            "is_compliant": is_clean,
            "failed_rules": 0 if is_clean else 3,
            "failed_checks": 0 if is_clean else 19,
            "structure": {"pages": 4},
            "findings": [] if is_clean else [{"clause": "6.2"}, {"clause": "7.1"}, {"clause": "5"}],
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ActionRunnerTest(unittest.TestCase):
    def test_end_to_end_outputs_summary_and_exit_codes(self) -> None:
        import tempfile

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockCheckerHandler)
        port = srv.server_port
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                (ws / "docs").mkdir()
                (ws / "docs" / "pass.pdf").write_bytes(b"%PDF-1.7\n%CLEAN_SPECIMEN\n%%EOF\n")
                (ws / "docs" / "fail.pdf").write_bytes(b"%PDF-1.7\n%UNTAGGED\n%%EOF\n")
                out_file = ws / "gh_output.txt"
                sum_file = ws / "gh_summary.md"

                rc_fail = action_runner.run_check(
                    pdf_path="docs",
                    api_url=f"http://127.0.0.1:{port}/api/check",
                    fail_on_violations=True,
                    max_files=10,
                    workspace=ws,
                    github_output=str(out_file),
                    github_summary=str(sum_file),
                )
                self.assertEqual(rc_fail, 1)

                out_text = out_file.read_text(encoding="utf-8")
                self.assertIn("total-pdfs=2", out_text)
                self.assertIn("failing-pdfs=1", out_text)
                self.assertIn("failed-rules=3", out_text)

                sum_text = sum_file.read_text(encoding="utf-8")
                self.assertIn("docs/pass.pdf", sum_text)
                self.assertIn("docs/fail.pdf", sum_text)
                self.assertIn("https://pdfuapartnerdesk.gumroad.com/l/pdfua-express-5-pages?wanted=true", sum_text)
                self.assertIn("https://hadesloc.github.io/pdfua-partner-desk/", sum_text)

                rc_warn_only = action_runner.run_check(
                    pdf_path="docs",
                    api_url=f"http://127.0.0.1:{port}/api/check",
                    fail_on_violations=False,
                    max_files=10,
                    workspace=ws,
                )
                self.assertEqual(rc_warn_only, 0)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
