import csv
import io
import unittest

from lead_export import scraped_leads_csv


class LeadExportTests(unittest.TestCase):
    def test_exports_only_email_contacts_with_excel_compatible_header(self):
        data = scraped_leads_csv([
            {"email": "hello@example.com", "company_name": "A, B Ltd",
             "website": "https://example.com"},
            {"email": None, "company_name": "No Email"},
        ], business_type="shops", location="Delhi")

        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "hello@example.com")
        self.assertEqual(rows[0]["company"], "A, B Ltd")
        self.assertEqual(rows[0]["business_type"], "shops")
        self.assertEqual(rows[0]["location"], "Delhi")


if __name__ == "__main__":
    unittest.main()
