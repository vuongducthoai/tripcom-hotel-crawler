"""Kiểm tra lớp chặn của crawl_jobs: web KHÔNG được phép chạy lệnh tuỳ ý.

Trọng tâm:
  - Job lạ bị từ chối.
  - Mọi tham số số/chuỗi đều bị ép về khoảng cho phép.
  - argv luôn là danh sách, script nằm trong repo, không có shell.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import crawl_jobs  # noqa: E402


class JobSpecTests(unittest.TestCase):
    def test_job_keys_are_fixed(self):
        self.assertEqual(
            sorted(crawl_jobs.JOB_SPECS),
            ["amenities", "audit", "detail", "loader", "overview", "reparse"],
        )

    def test_every_spec_points_at_a_real_script(self):
        for key, spec in crawl_jobs.JOB_SPECS.items():
            argv = spec["build"]({})
            script = ROOT / argv[0]
            self.assertTrue(script.is_file(), f"{key}: thiếu {argv[0]}")

    def test_unknown_job_is_refused(self):
        runner = crawl_jobs.JobRunner()
        with self.assertRaises(ValueError):
            runner.start("rm", {})
        with self.assertRaises(ValueError):
            runner.start("src/crawl_detail.py", {})


class ArgumentValidationTests(unittest.TestCase):
    def test_locale_must_be_known(self):
        for bad in ("fr-FR", "vi-VN; rm -rf /", "--help", "vi-VN\n--currency=X"):
            with self.assertRaises(ValueError, msg=bad):
                crawl_jobs._build_detail({"locale": bad})

    def test_blank_locale_falls_back_to_default(self):
        # Ô trống trên web = dùng mặc định, không phải lỗi.
        for blank in ("", None):
            self.assertIn("vi-VN", crawl_jobs._build_detail({"locale": blank}))

    def test_locale_default_pairs_with_currency(self):
        self.assertEqual(crawl_jobs._market({}), ["--locale", "vi-VN", "--currency", "VND"])
        self.assertEqual(
            crawl_jobs._market({"locale": "en-US"}),
            ["--locale", "en-US", "--currency", "USD"],
        )

    def test_currency_must_be_known(self):
        with self.assertRaises(ValueError):
            crawl_jobs._market({"locale": "vi-VN", "currency": "BTC"})

    def test_workers_are_capped(self):
        with self.assertRaises(ValueError):
            crawl_jobs._build_detail({"workers": 9})
        with self.assertRaises(ValueError):
            crawl_jobs._build_detail({"workers": 0})
        self.assertIn("3", crawl_jobs._build_detail({"workers": 3}))

    def test_non_numeric_numbers_are_refused(self):
        for bad in ("2; ls", "1 || true", None, [2], {"a": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                crawl_jobs._int_arg(bad, "workers", 1, 3)

    def test_limit_and_city_ranges(self):
        with self.assertRaises(ValueError):
            crawl_jobs._build_detail({"limit": 10 ** 9})
        with self.assertRaises(ValueError):
            crawl_jobs._build_overview({"city_id": -1})
        self.assertEqual(
            crawl_jobs._build_overview({"city_id": 301}),
            ["src/crawl_api.py", "--city-id", "301"],
        )

    def test_profile_dir_rejects_paths(self):
        for bad in ("../../etc", "a/b", "prof;ls", "x" * 65, "prof dir"):
            with self.assertRaises(ValueError, msg=bad):
                crawl_jobs._build_detail({"profile_dir": bad})
        argv = crawl_jobs._build_detail({"profile_dir": "browser_profile_en-US_USD"})
        self.assertIn("browser_profile_en-US_USD", argv)

    def test_flags_are_literal_not_user_text(self):
        argv = crawl_jobs._build_detail({"missing_only": "bất kỳ giá trị nào"})
        self.assertIn("--missing-only", argv)
        # Giá trị người dùng gửi không bao giờ lọt vào argv nguyên văn.
        self.assertNotIn("bất kỳ giá trị nào", argv)

    def test_every_argv_element_is_a_plain_string(self):
        params = {"locale": "en-US", "city_id": 301, "workers": 2, "limit": 50,
                  "missing_only": True, "apply": True, "max_pages": 3, "no_prices": True}
        for key, spec in crawl_jobs.JOB_SPECS.items():
            argv = spec["build"](params)
            for item in argv:
                self.assertIsInstance(item, str, f"{key}: {item!r}")
                self.assertNotIn(";", item)
                self.assertNotIn("&", item)
                self.assertNotIn("|", item)


class ProgressParsingTests(unittest.TestCase):
    def _parse(self, line: str):
        for pattern in crawl_jobs.PROGRESS_PATTERNS:
            found = pattern.search(line)
            if found:
                return int(found.group(1)), int(found.group(2))
        return None

    def test_bracket_progress(self):
        self.assertEqual(self._parse("[2617/3431] 1234567 OK"), (2617, 3431))

    def test_vietnamese_progress(self):
        self.assertEqual(self._parse("Tiến độ: 750/1491 raw (50%)"), (750, 1491))

    def test_plain_line_has_no_progress(self):
        self.assertIsNone(self._parse("Đang mở trình duyệt..."))


if __name__ == "__main__":
    unittest.main()
