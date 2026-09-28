import unittest
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import link_verification
from server import check_answer_links, init_db


class LinkVerificationTests(unittest.TestCase):
    def test_extract_deduplicates_cited_links_and_keeps_context(self):
        text = "이 방법을 권장합니다. ([자료](https://example.org/report)) 다시 https://example.org/report"
        links, skipped = link_verification.extract_citations(text)
        self.assertEqual([link["url"] for link in links], ["https://example.org/report"])
        self.assertIn("권장", links[0]["claimContext"])
        self.assertEqual(skipped, 0)

    def test_private_targets_and_redirects_are_blocked(self):
        with patch("link_verification.socket.getaddrinfo", return_value=[
                (None, None, None, None, ("127.0.0.1", 443))]):
            self.assertRaisesRegex(ValueError, "비공개", link_verification._public_ip, "example.org", 443)
        self.assertEqual(link_verification.verify_url("https://127.0.0.1/private")["status"], "blocked")
        with patch("link_verification._download", return_value=(302, "text/html", b"", "https://localhost/secret", False)):
            result = link_verification.verify_url("https://example.org/start")
        self.assertEqual(result["status"], "blocked")

    def test_html_body_is_a_snapshot_not_a_claim_verdict(self):
        html = ("<html><head><title>보고서</title><style>숨김</style></head><body>"
                "<p>퍼실리테이션의 효과를 측정하는 방법을 설명합니다.</p>"
                "<script>무시</script><p>회의 전 목표를 정하고 종료 후 실행을 확인합니다. "
                "참가자들이 남긴 후속 과제를 다음 회의에서 다시 점검하면 개선 여부도 살펴볼 수 있습니다.</p></body></html>")
        with patch("link_verification._download", return_value=(200, "text/html", html.encode(),
                                                                "https://example.org/report", False)):
            result = link_verification.verify_url("https://example.org/report")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["title"], "보고서")
        self.assertNotIn("숨김", result["excerpt"])
        self.assertNotIn("무시", result["excerpt"])
        self.assertIn("회의 전 목표", result["excerpt"])
        long_html = ("<html><body>" + "회의 목표와 실행 결과를 확인합니다. " * 500 + "</body></html>").encode()
        with patch("link_verification._download", return_value=(200, "text/html", long_html,
                                                                "https://example.org/report", False)):
            limited = link_verification.verify_url("https://example.org/report")
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["excerpt"]), link_verification.MAX_EXCERPT)

    def test_link_result_is_cached_for_repeat_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            init_db(db_path)
            item = {"url": "https://example.org/report", "checkerVersion": link_verification.CACHE_VERSION,
                    "checkedAt": datetime.now(UTC).isoformat(),
                    "status": "verified", "finalUrl": "https://example.org/report", "excerpt": "본문"}
            with patch("server.link_verification.verify_url", return_value=item) as verify:
                first = check_answer_links(db_path, "자료 https://example.org/report")
                second = check_answer_links(db_path, "다른 문맥 https://example.org/report")
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(first["items"][0]["status"], "verified")
            self.assertNotEqual(first["items"][0]["claimContext"], second["items"][0]["claimContext"])

    def test_pdf_text_is_available_as_cited_source(self):
        page = SimpleNamespace(extract_text=lambda: "회의 전 목표와 참가자별 발언 기회를 정합니다. " * 4)
        reader = SimpleNamespace(is_encrypted=False, pages=[page], metadata=SimpleNamespace(title="회의 보고서"))
        with (patch("link_verification._download", return_value=(200, "application/pdf", b"%PDF-example",
                                                                 "https://example.org/report.pdf", False)),
              patch("pypdf.PdfReader", return_value=reader)):
            result = link_verification.verify_url("https://example.org/report.pdf")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["title"], "회의 보고서")
        self.assertIn("참가자별 발언", result["excerpt"])


if __name__ == "__main__":
    unittest.main()
