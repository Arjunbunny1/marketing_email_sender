"""Offline checks for pausing and resuming each campaign stage."""

import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


RUNNER_PATH = Path(__file__).resolve().parents[1] / "campaign_runner.py"


class CampaignResumeTests(unittest.TestCase):
    def setUp(self):
        self.campaign = None
        self.sent = []
        self.lead_id = 0
        self.scrape = None
        self.lookup = None
        self.compose = None
        self.send = None

        database = types.ModuleType("database")

        def create_campaign(business_type, location, offering, company_name,
                            company_website):
            self.campaign = {
                "id": 1, "business_type": business_type, "location": location,
                "offering": offering, "company_name": company_name,
                "company_website": company_website, "status": "running",
            }
            return 1

        def save_paused_campaign(campaign_id, records, sent_count=0,
                                 total_count=0, resume_stage="", resume_payload=None):
            self.campaign.update({
                "status": "paused", "unsent_records": copy.deepcopy(records),
                "sent_count": sent_count, "total_count": total_count,
                "resume_stage": resume_stage,
                "resume_data": copy.deepcopy(resume_payload or {}),
            })

        def deduplicate_leads(leads, skip_already_sent=True):
            seen = set()
            kept = []
            for lead in leads:
                email = lead.get("email")
                if email and email in seen:
                    continue
                if email:
                    seen.add(email)
                kept.append(lead)
            return kept, {"kept": len(kept), "total": len(leads)}

        def insert_lead(**_kwargs):
            self.lead_id += 1
            return self.lead_id

        def insert_email_sent(**kwargs):
            self.sent.append(kwargs["recipient_email"])

        database.create_campaign = create_campaign
        database.save_paused_campaign = save_paused_campaign
        database.get_paused_campaign = lambda _id: copy.deepcopy(self.campaign) if self.campaign and self.campaign["status"] == "paused" else None
        database.get_paused_campaigns = lambda: []
        database.update_campaign_status = lambda _id, status: self.campaign.update(status=status)
        database.deduplicate_leads = deduplicate_leads
        database.insert_lead = insert_lead
        database.insert_email_sent = insert_email_sent
        database.has_initial_email_sent = lambda recipient: recipient in self.sent

        maps = types.ModuleType("maps_scraper")
        maps.scrape_google_maps = lambda **kwargs: self.scrape(**kwargs)
        finder = types.ModuleType("email_finder")
        finder.batch_find_emails = lambda leads, **kwargs: self.lookup(leads, **kwargs)
        verifier = types.ModuleType("email_verifier")
        verifier.filter_leads_by_valid_email = lambda leads: (leads, [])
        writer = types.ModuleType("ai_writer")
        writer.generate_cold_email = lambda **kwargs: self.compose(**kwargs)
        smtp = types.ModuleType("smtp_sender")

        class FakeSMTP:
            def __init__(_self, _senders):
                pass

            def send_email(_self, **kwargs):
                return self.send(**kwargs)

        smtp.SMTPSender = FakeSMTP
        utils = types.ModuleType("utils")
        for name in ("log_info", "log_error", "log_warning", "clear_log_buffer"):
            setattr(utils, name, lambda *_args: None)

        modules = {"database": database, "maps_scraper": maps,
                   "email_finder": finder, "email_verifier": verifier,
                   "ai_writer": writer, "smtp_sender": smtp, "utils": utils}
        spec = importlib.util.spec_from_file_location("campaign_runner_under_test", RUNNER_PATH)
        self.runner = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.runner)

        def synchronous_spawn(target, args, setup=None):
            if setup:
                setup()
            target(*args)
            return True

        self.runner._spawn = synchronous_spawn
        self.compose = lambda **kwargs: {
            "subject": f"Hello {kwargs['company_name']}", "body": "Test body",
        }
        self.send = lambda **kwargs: {
            "success": True, "sender_email": "sender@example.com",
            "message_id": "test-message-id",
        }

    def params(self):
        return {
            "source": "scraper", "business_type": "shops", "location": "Delhi",
            "max_scrolls": 2, "offering": "service", "company_name": "Sender Co",
            "company_website": "", "ai_api_key": "", "custom_subject": "",
            "custom_body": "", "skip_already_sent": True,
            "senders": [{"email": "sender@example.com", "password": "secret", "name": "S"}],
        }

    @staticmethod
    def leads():
        return [{"company_name": "A", "website": "https://a.example"},
                {"company_name": "B", "website": "https://b.example"}]

    def complete_lookup(self, leads, **kwargs):
        for index, lead in enumerate(leads, 1):
            if not lead.get("email_lookup_done"):
                lead["email"] = f"{lead['company_name'].lower()}@example.com"
                lead["email_lookup_done"] = True
                kwargs["progress_callback"](index, leads)
        return leads

    def resume(self):
        return self.runner.resume_campaign(
            1, self.params()["senders"], offering="service", company_name="Sender Co",
        )

    def prepare_saved_leads(self):
        return self.runner.resume_campaign(
            1, self.params()["senders"], offering="service", company_name="Sender Co",
            start_ready=True,
        )

    def test_pause_in_maps_keeps_listings_and_resumes(self):
        calls = []

        def scrape(**kwargs):
            calls.append([lead["company_name"] for lead in kwargs["existing_leads"]])
            result = list(kwargs["existing_leads"])
            result.append(self.leads()[len(result)])
            kwargs["progress_callback"](result)
            if len(calls) == 1:
                kwargs["stop_event"].set()
            else:
                result.append(self.leads()[1])
                kwargs["progress_callback"](result)
            return result

        self.scrape = scrape
        self.lookup = self.complete_lookup
        self.runner.start_campaign(self.params())
        self.assertEqual(self.campaign["resume_stage"], "scraping")
        self.assertTrue(self.resume())
        self.assertEqual(calls, [[], ["A"]])
        self.assertEqual(self.runner.snapshot()["status"], "leads_ready")
        self.assertEqual(self.sent, [])
        self.assertTrue(self.prepare_saved_leads())
        self.assertEqual(self.runner.snapshot()["status"], "awaiting_review")
        self.assertEqual(len(self.runner.snapshot()["prepared"]), 2)

    def test_pause_in_email_lookup_skips_completed_site(self):
        self.scrape = lambda **kwargs: self.leads()
        visits = []

        def lookup(leads, **kwargs):
            for index, lead in enumerate(leads, 1):
                if lead.get("email_lookup_done"):
                    continue
                visits.append(lead["company_name"])
                lead["email"] = f"{lead['company_name'].lower()}@example.com"
                lead["email_lookup_done"] = True
                kwargs["progress_callback"](index, leads)
                if len(visits) == 1:
                    kwargs["stop_event"].set()
                    break
            return leads

        self.lookup = lookup
        self.runner.start_campaign(self.params())
        self.assertEqual(self.campaign["resume_stage"], "finding_emails")
        self.assertTrue(self.resume())
        self.assertEqual(visits, ["A", "B"])
        self.assertEqual(self.runner.snapshot()["status"], "leads_ready")
        self.assertTrue(self.prepare_saved_leads())
        self.assertEqual(len(self.runner.snapshot()["prepared"]), 2)

    def test_pause_in_composition_continues_without_duplicate(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        composed = []

        def compose(**kwargs):
            composed.append(kwargs["company_name"])
            if len(composed) == 1:
                self.runner.request_stop()
            return {"subject": "Subject", "body": "Body"}

        self.compose = compose
        self.runner.start_campaign(self.params())
        self.assertEqual(self.campaign["resume_stage"], "leads_ready")
        self.assertEqual(composed, [])
        self.assertTrue(self.prepare_saved_leads())
        self.assertEqual(self.campaign["resume_stage"], "composing")
        self.assertEqual(self.campaign["resume_data"]["next_index"], 1)
        self.assertEqual(len(self.campaign["resume_data"]["prepared"]), 1)
        self.assertTrue(self.resume())
        self.assertEqual(composed, ["A", "B"])
        self.assertEqual(len(self.runner.snapshot()["prepared"]), 2)

    def test_pause_while_saving_leads_does_not_insert_twice(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        insert_lead = self.runner.insert_lead

        def stop_after_first(**kwargs):
            lead_id = insert_lead(**kwargs)
            if lead_id == 1:
                self.runner.request_stop()
            return lead_id

        self.runner.insert_lead = stop_after_first
        self.runner.start_campaign(self.params())
        self.assertEqual(self.campaign["resume_stage"], "finding_emails")
        self.assertEqual(self.campaign["unsent_records"][0]["db_id"], 1)
        self.assertTrue(self.resume())
        self.assertEqual(self.lead_id, 2)
        self.assertEqual(self.runner.snapshot()["status"], "leads_ready")
        self.assertTrue(self.prepare_saved_leads())
        self.assertEqual(len(self.runner.snapshot()["prepared"]), 2)

    def test_review_restores_without_sending(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        self.runner.start_campaign(self.params())
        self.assertTrue(self.prepare_saved_leads())
        self.assertEqual(self.campaign["resume_stage"], "review")
        self.runner.reset()
        self.assertTrue(self.resume())
        self.assertEqual(self.runner.snapshot()["status"], "awaiting_review")
        self.assertEqual(self.sent, [])

    def test_pause_during_sending_resumes_remaining_only(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        self.runner.start_campaign(self.params())
        self.assertTrue(self.prepare_saved_leads())
        prepared = self.runner.snapshot()["prepared"]

        def send(**kwargs):
            if not self.sent:
                self.runner.request_stop()
            return {"success": True, "sender_email": "sender@example.com",
                    "message_id": "test-message-id"}

        self.send = send
        self.runner.start_send(prepared, self.params()["senders"], 1)
        self.assertEqual(self.campaign["resume_stage"], "sending")
        self.assertEqual(self.campaign["unsent_records"][0]["recipient"], "b@example.com")
        self.assertTrue(self.resume())
        self.assertEqual(self.sent, ["a@example.com", "b@example.com"])
        self.assertEqual(self.campaign["status"], "complete")

    def test_saved_leads_reopen_without_composing_or_sending(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        self.runner.start_campaign(self.params())
        self.runner.reset()  # Save for later closes the current results view.
        self.assertTrue(self.resume())
        self.assertEqual(self.runner.snapshot()["status"], "leads_ready")
        self.assertEqual(self.campaign["resume_stage"], "leads_ready")
        self.assertEqual(self.sent, [])

    def test_saved_leads_use_template_chosen_when_preparing(self):
        self.scrape = lambda **kwargs: self.leads()
        self.lookup = self.complete_lookup
        composed_with = []

        def compose(**kwargs):
            composed_with.append((kwargs["custom_subject"], kwargs["custom_body"]))
            return {"subject": "Subject", "body": "Body"}

        self.compose = compose
        self.runner.start_campaign(self.params())
        self.runner.reset()
        self.assertTrue(self.resume())
        self.assertTrue(self.runner.resume_campaign(
            1, self.params()["senders"], start_ready=True,
            custom_subject="Hello {company_name}", custom_body="Interested?",
        ))
        self.assertEqual(composed_with, [
            ("Hello {company_name}", "Interested?"),
            ("Hello {company_name}", "Interested?"),
        ])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
