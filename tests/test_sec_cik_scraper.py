import json
import unittest
from unittest.mock import Mock, call, patch

with patch("boto3.client"):
    from scrapers import sec_cik_scraper


def response(status, text=""):
    return Mock(status_code=status, text=text)


PAGE = """
<table class="tableFile2">
  <tr><td><a href="?CIK=123">123</a></td><td>Masterworks Example</td></tr>
</table>
"""


class CikScraperTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.s3 = Mock()
        self.session_patch = patch.object(
            sec_cik_scraper.requests, "Session", return_value=self.session
        )
        self.s3_patch = patch.object(sec_cik_scraper, "S3_CLIENT", self.s3)
        self.sleep_patch = patch.object(sec_cik_scraper.time, "sleep")
        self.session_patch.start()
        self.s3_patch.start()
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.s3_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    def test_retries_503_and_writes_complete_results(self):
        self.session.get.side_effect = [
            response(503), response(200, PAGE), response(200, PAGE)
        ]

        result = sec_cik_scraper.run()

        self.assertEqual(result, {"123": "Masterworks Example"})
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.session.get.call_args_list[0].kwargs["params"]["start"], 0)
        self.assertEqual(self.session.get.call_args_list[1].kwargs["params"]["start"], 0)
        self.assertEqual(self.session.get.call_args_list[2].kwargs["params"]["start"], 40)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(0.5)])
        self.assertEqual(json.loads(self.s3.put_object.call_args.kwargs["Body"]), result)

    def test_persistent_503_on_later_page_does_not_write_partial_results(self):
        self.session.get.side_effect = [
            response(200, PAGE), response(503), response(503), response(503)
        ]

        with self.assertRaisesRegex(RuntimeError, r"HTTP 503.*start=40"):
            sec_cik_scraper.run()

        self.assertEqual(self.session.get.call_count, 4)
        self.s3.put_object.assert_not_called()

    def test_non_retryable_error_does_not_write_partial_results(self):
        self.session.get.side_effect = [response(200, PAGE), response(403)]

        with self.assertRaisesRegex(RuntimeError, r"HTTP 403.*start=40"):
            sec_cik_scraper.run()

        self.assertEqual(self.session.get.call_count, 2)
        self.s3.put_object.assert_not_called()

    def test_empty_results_do_not_overwrite_s3(self):
        self.session.get.return_value = response(200, "<html></html>")

        with self.assertRaisesRegex(RuntimeError, "Aucun CIK"):
            sec_cik_scraper.run()

        self.s3.put_object.assert_not_called()

    def test_connection_error_does_not_write_partial_results(self):
        self.session.get.side_effect = [
            response(200, PAGE), sec_cik_scraper.requests.ConnectionError("network")
        ]

        with self.assertRaises(sec_cik_scraper.requests.ConnectionError):
            sec_cik_scraper.run()

        self.s3.put_object.assert_not_called()

    def test_automated_tool_block_does_not_write_partial_results(self):
        self.session.get.side_effect = [
            response(200, PAGE), response(200, "Undeclared Automated Tool")
        ]

        with self.assertRaisesRegex(RuntimeError, "SEC_USER_AGENT"):
            sec_cik_scraper.run()

        self.s3.put_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
