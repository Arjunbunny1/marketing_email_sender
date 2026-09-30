"""
campaign_runner.py — runs campaigns on a background thread.

WHY THIS EXISTS
    Streamlit terminates an in-flight script run whenever a rerun is
    triggered, by raising StopException at the script's next Streamlit API
    call. A campaign that ran inside the script run was therefore killed by
    any incidental browser event — a click, a tab refocus, a websocket
    reconnect. A six-minute scrape could be destroyed silently, losing every
    lead, because the very next line after scraping touched st.session_state.

    Doing the work on a daemon thread fixes that: the pipeline no longer
    depends on the browser staying still, the UI stays responsive, progress
    updates live, and STOP actually stops something.

THE RULE FOR THIS MODULE
    Nothing here may touch `st.` anything. A background thread has no script
    context, so Streamlit calls from it either fail or are ignored. All state
    is kept in the module-level _STATE object behind a lock, and the UI polls
    snapshot() for a plain dict.
"""

import threading
from datetime import datetime

from database import (
    insert_lead, insert_email_sent, create_campaign, update_campaign_status,
    deduplicate_leads, has_initial_email_sent, save_paused_campaign,
    get_paused_campaign, get_paused_campaigns,
)
from maps_scraper import scrape_google_maps
from email_finder import batch_find_emails
from email_verifier import filter_leads_by_valid_email
from ai_writer import generate_cold_email
from smtp_sender import SMTPSender
from utils import log_info, log_error, log_warning, clear_log_buffer


# ── STATE ──────────────────────────────────────────────────────────────────────
# status transitions:
#   idle → running → leads_ready → running → awaiting_review → sending → done
#                 ↘ error / cancelled / paused ↙

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self._reset_fields()

    def _reset_fields(self):
        self.status      = "idle"
        self.step        = ""
        self.detail      = ""
        self.leads_found = 0
        self.done        = 0
        self.total       = 0
        self.sent        = 0
        self.error       = None
        self.prepared    = []
        self.campaign_id = None
        self.source      = ""
        self.started_at  = None
        self.stop_event  = threading.Event()

    def reset(self):
        with self.lock:
            self._reset_fields()

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot_stop_event(self) -> threading.Event:
        """The live Event, so a worker sees a stop requested after it started."""
        with self.lock:
            return self.stop_event

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status, "step": self.step, "detail": self.detail,
                "leads_found": self.leads_found, "done": self.done,
                "total": self.total, "sent": self.sent, "error": self.error,
                "prepared": list(self.prepared), "campaign_id": self.campaign_id,
                "source": self.source, "started_at": self.started_at,
                "stopping": self.stop_event.is_set(),
            }


_STATE = _State()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def snapshot() -> dict:
    return _STATE.snapshot()


def is_busy() -> bool:
    return _STATE.snapshot()["status"] in ("running", "sending")


def request_stop() -> None:
    """Ask the running pipeline to pause at its next checkpoint."""
    with _STATE.lock:
        _STATE.stop_event.set()
    log_info("Pause requested — finishing current item then pausing campaign.")


def reset() -> None:
    """Clear state back to idle (after the user dismisses a result)."""
    _STATE.reset()


def _fail(campaign_id, message):
    log_error(f"Campaign error: {message}")
    if campaign_id:
        try:
            update_campaign_status(campaign_id, "error")
        except Exception:
            pass
    _STATE.update(status="error", error=message)


def _pause_state(campaign_id, prepared=None, sent=0, total=0, detail="Campaign paused.", persist: bool = False):
    prepared = prepared or []
    if campaign_id and persist:
        try:
            save_paused_campaign(campaign_id, prepared, sent_count=sent, total_count=total)
        except Exception:
            pass
    _STATE.update(
        status="paused", prepared=prepared, sent=sent, total=total,
        step="Campaign paused",
        detail=detail,
    )
    log_info(f"Campaign #{campaign_id} paused.")


def _safe_params(params):
    """Persist campaign settings only; credentials must never enter SQLite."""
    return {k: v for k, v in params.items() if k not in (
        "senders", "contacts", "ai_api_key", "resume_records", "prepared_prefix",
        "saved_scraped_leads",
    )}


def _checkpoint(campaign_id, stage, records, params, **extra):
    """Persist enough state to resume this exact pipeline stage after restart."""
    payload = {"params": _safe_params(params), "records": records, **extra}
    save_paused_campaign(campaign_id, records, 0, len(records), stage, payload)


# ── COMPOSE ────────────────────────────────────────────────────────────────────

def _prepare_emails(contacts, senders, ai_api_key, offering, company_name,
                    custom_subject, custom_body, stop_event,
                    prepared=None, start_index=0, progress_callback=None):
    """Compose remaining contacts and return (messages, next contact index)."""
    active = [s for s in senders if s.get("email") and s.get("password")]
    sender_name = active[0].get("name", "") if active else ""

    prepared = list(prepared or [])
    total = len(contacts)
    _STATE.update(total=total, done=start_index)
    next_index = start_index

    for index in range(start_index, total):
        if stop_event.is_set():
            log_info("Composition paused by user.")
            break
        contact = contacts[index]
        i = index + 1

        recipient = contact.get("email")
        if not recipient:
            next_index = i
            if progress_callback:
                progress_callback(prepared, next_index)
            continue

        first_name   = contact.get("first_name", "")
        last_name    = contact.get("last_name", "")
        contact_name = contact.get("company_name") or first_name or "there"

        _STATE.update(done=i, detail=f"Composing for {recipient}")

        content = generate_cold_email(
            api_key=ai_api_key, company_name=contact_name, offering=offering,
            sender_name=sender_name, sender_company=company_name,
            custom_subject=custom_subject, custom_body=custom_body,
            first_name=first_name, last_name=last_name,
        )
        if not content:
            next_index = i
            if progress_callback:
                progress_callback(prepared, next_index)
            continue

        prepared.append({
            "id":           f"{i}:{recipient}",
            "recipient":    recipient,
            "display_name": first_name or contact_name,
            "company_name": contact_name,
            "db_id":        contact.get("db_id"),
            "subject":      content["subject"],
            "body":         content["body"],
        })
        next_index = i
        if progress_callback:
            progress_callback(prepared, next_index)

    return prepared, next_index


def _deduplicate_scraped_businesses(leads):
    """Remove repeated Maps listings before website and email lookups."""
    unique = []
    seen = set()
    for lead in leads:
        company = (lead.get("company_name") or "").strip().lower()
        website = (lead.get("website") or "").strip().lower().rstrip("/")
        # A website is the reliable identity before an email is found.  Keep
        # entries without one; address-level de-duplication happens later.
        if not website:
            unique.append(lead)
            continue
        key = (company, website)
        if key not in seen:
            seen.add(key)
            unique.append(lead)
    return unique


# ── PIPELINE ───────────────────────────────────────────────────────────────────

def _pipeline(params):
    """Gather contacts and compose emails, then wait for human approval."""
    campaign_id = None
    stop_event  = _STATE.snapshot_stop_event()
    try:
        clear_log_buffer()
        source = params["source"]

        resuming = bool(params.get("campaign_id"))
        campaign_id = params.get("campaign_id") or create_campaign(
            params.get("business_type", "list_import"), params.get("location", "uploaded_list"),
            params.get("offering", ""), params.get("company_name", ""), params.get("company_website", ""))
        _STATE.update(campaign_id=campaign_id, source=source)
        log_info(f"Campaign #{campaign_id} {'resumed' if resuming else 'started'} ({source}).")

        stage = params.get("resume_stage") or ""
        records = list(params.get("resume_records") or [])

        if source == "scraper":
            if stage in ("", "scraping"):
                _STATE.update(step="Step 1/3 — Scraping Google Maps…")

                def save_scraped(current):
                    _STATE.update(leads_found=len(current),
                                  detail=f"{len(current)} businesses saved so far")
                    _checkpoint(campaign_id, "scraping", current, params)

                leads = scrape_google_maps(
                    business_type=params["business_type"],
                    location=params["location"],
                    max_scrolls=params["max_scrolls"],
                    headless=True, stop_event=stop_event,
                    existing_leads=records,
                    progress_callback=save_scraped,
                )
                if stop_event.is_set():
                    _checkpoint(campaign_id, "scraping", leads, params)
                    return _pause_state(
                        campaign_id, leads, total=len(leads),
                        detail="Paused in Maps. Resume will revisit the results and skip businesses already saved.",
                    )
                leads = _deduplicate_scraped_businesses(leads)
                if not leads:
                    return _fail(campaign_id, "No businesses found. Try different terms or a broader location.")
                records = leads
                _checkpoint(campaign_id, "finding_emails", records, params)

            if stage in ("", "scraping", "finding_emails"):
                leads = records
                _STATE.update(step=f"Step 2/3 — Finding emails on {len(leads)} websites…",
                              leads_found=len(leads), done=0, total=len(leads))

                def save_email_lookup(index, current):
                    _STATE.update(done=index, detail=f"Checked {index}/{len(current)} websites")
                    _checkpoint(campaign_id, "finding_emails", current, params)

                batch_find_emails(
                    leads, max_leads=len(leads), stop_event=stop_event,
                    progress_callback=save_email_lookup,
                )
                if stop_event.is_set():
                    _checkpoint(campaign_id, "finding_emails", leads, params)
                    return _pause_state(
                        campaign_id, leads, total=len(leads),
                        detail="Paused during email lookup. Resume will continue with unprocessed websites.",
                    )

                leads, stats = deduplicate_leads(
                    leads, skip_already_sent=params.get("skip_already_sent", True),
                )
                log_info(f"Email lookup complete — {stats['kept']}/{stats['total']} leads kept.")
                for lead in leads:
                    if lead.get("company_name") and not lead.get("db_id"):
                        lead["db_id"] = insert_lead(
                            company_name=lead["company_name"],
                            website=lead.get("website"), email=lead.get("email"),
                            location=params.get("location", ""),
                            business_type=params.get("business_type", "scraper"),
                        )
                        _checkpoint(campaign_id, "finding_emails", leads, params)
                    if stop_event.is_set():
                        _checkpoint(campaign_id, "finding_emails", leads, params)
                        return _pause_state(
                            campaign_id, leads, total=len(leads),
                            detail="Paused while saving leads. Resume will finish saving them.",
                        )
                contacts = [
                    {"email": lead.get("email"), "first_name": "", "last_name": "",
                     "company_name": lead["company_name"], "website": lead.get("website"),
                     "db_id": lead.get("db_id")}
                    for lead in leads if lead.get("company_name")
                ]
                sendable = sum(bool(contact.get("email")) for contact in contacts)
                _checkpoint(campaign_id, "leads_ready", contacts, params,
                            sendable_count=sendable)
                _STATE.update(
                    status="leads_ready", leads_found=len(contacts),
                    step=f"{sendable} email address(es) found — choose what to do next",
                    detail="No messages have been composed or sent.",
                )
                log_info(f"Scrape complete — {sendable} email address(es) found. Awaiting your choice.")
                return
            else:
                contacts = records

        elif source == "list":
            if stage == "composing":
                contacts = records
            else:
                contacts, stats = deduplicate_leads(
                    list(params["contacts"]),
                    skip_already_sent=params.get("skip_already_sent", True),
                )
                if not contacts:
                    return _fail(campaign_id, "No unsent contacts remain in this list.")
                for contact in contacts:
                    if not contact.get("db_id"):
                        contact["db_id"] = insert_lead(
                            company_name=contact.get("company_name", ""),
                            website=contact.get("website"), email=contact.get("email"),
                            location="", business_type="list_import",
                        )
                _checkpoint(campaign_id, "composing", contacts, params,
                            prepared=[], next_index=0)
        else:
            return _fail(campaign_id, f"Unknown campaign source: {source}")

        _STATE.update(leads_found=len(contacts),
                      step="Step 3/3 — Composing emails for review…")
        prefix = list(params.get("prepared_prefix") or [])
        next_index = int(params.get("next_index") or 0)
        _checkpoint(campaign_id, "composing", contacts, params,
                    prepared=prefix, next_index=next_index)
        prepared, next_index = _prepare_emails(
            contacts, params["senders"], params.get("ai_api_key", ""),
            params["offering"], params["company_name"],
            params["custom_subject"], params["custom_body"], stop_event,
            prepared=prefix, start_index=next_index,
            progress_callback=lambda current, index: _checkpoint(
                campaign_id, "composing", contacts, params,
                prepared=current, next_index=index),
        )
        if stop_event.is_set():
            _checkpoint(campaign_id, "composing", contacts, params,
                        prepared=prepared, next_index=next_index)
            return _pause_state(
                campaign_id, contacts[next_index:], total=len(contacts),
                detail=f"Paused during composition. {next_index}/{len(contacts)} contacts processed; resume continues with the rest.",
            )
        if not prepared:
            return _fail(campaign_id, "No emails could be composed.")

        _checkpoint(campaign_id, "review", prepared, params)
        _STATE.update(
            status="awaiting_review", prepared=prepared,
            step=f"{len(prepared)} email(s) ready for your review", detail="",
        )
        log_info(f"{len(prepared)} email(s) composed and awaiting review — nothing has been sent.")

    except Exception as exc:
        _fail(campaign_id, str(exc))


def _cancel(campaign_id):
    if campaign_id:
        try:
            update_campaign_status(campaign_id, "cancelled")
        except Exception:
            pass
    _STATE.update(status="cancelled", step="Cancelled", detail="")
    log_info("Campaign cancelled.")


# ── SEND ───────────────────────────────────────────────────────────────────────

def _send(records, senders, campaign_id):
    """Send exactly the approved emails, on the background thread."""
    stop_event = _STATE.snapshot_stop_event()
    try:
        active = [s for s in senders if s.get("email") and s.get("password")]
        smtp   = SMTPSender(active)
        total  = len(records)
        sent   = 0
        skipped_existing = 0
        _STATE.update(total=total, done=0, sent=0,
                      step=f"Sending {total} approved email(s)…")
        if campaign_id:
            save_paused_campaign(campaign_id, records, total_count=total,
                                 resume_stage="sending")

        for i, item in enumerate(records, 1):
            if stop_event.is_set():
                remaining = records[i - 1:]
                log_info(f"Sending paused by user — {sent} sent, {len(remaining)} remaining.")
                if campaign_id:
                    save_paused_campaign(campaign_id, remaining, sent_count=sent,
                                         total_count=total, resume_stage="sending")
                _STATE.update(
                    status="paused", prepared=remaining, sent=sent, total=total,
                    step=f"Paused — {sent} of {total} sent ({len(remaining)} remaining)",
                    detail="Campaign paused. Click 'Resume Sending' to continue.",
                )
                return

            _STATE.update(done=i, detail=f"Sending to {item['recipient']}")
            if has_initial_email_sent(item["recipient"]):
                skipped_existing += 1
                log_warning(
                    f"[{i}/{total}] SKIP {item['recipient']} — an initial campaign "
                    "email is already recorded for this recipient."
                )
                if campaign_id:
                    save_paused_campaign(campaign_id, records[i:], sent_count=sent,
                                         total_count=total, resume_stage="sending")
                continue

            result = smtp.send_email(
                to_email=item["recipient"], subject=item["subject"],
                body=item["body"], company_name=item.get("company_name", ""),
            )
            if result["success"]:
                insert_email_sent(
                    lead_id=item.get("db_id"),
                    company_name=item.get("company_name", ""),
                    recipient_email=item["recipient"],
                    sender_email=result["sender_email"],
                    subject=item["subject"], body=item["body"],
                    campaign_id=campaign_id,
                    message_id=result.get("message_id"),
                )
                sent += 1
                _STATE.update(sent=sent)

            log_info(f"[{i}/{total}] {'OK' if result['success'] else 'FAIL'} "
                     f"{item['display_name'][:40]} <{item['recipient']}>")
            if campaign_id:
                save_paused_campaign(campaign_id, records[i:], sent_count=sent,
                                     total_count=total, resume_stage="sending")
            if stop_event.wait(1.5):
                continue

        if campaign_id:
            try:
                update_campaign_status(campaign_id, "complete")
            except Exception:
                pass

        summary = f"Sent {sent} of {total}"
        if skipped_existing:
            summary += f" ({skipped_existing} duplicate recipient(s) skipped)"
        _STATE.update(status="done", sent=sent, prepared=[], step=summary, detail="")
        log_info(f"Campaign complete — {summary}.")

    except Exception as exc:
        _fail(campaign_id, str(exc))


# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def _spawn(target, args, setup=None):
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            # A paused worker publishes its final state just before exiting.
            # Let it finish so an immediate Resume click can start cleanly.
            if is_busy():
                return False
            _thread.join(timeout=2)
            if _thread.is_alive():
                return False
        if setup is not None:
            setup()
        _STATE.update(started_at=datetime.now().strftime("%H:%M:%S"))
        _thread = threading.Thread(target=target, args=args, daemon=True,
                                   name="campaign-runner")
        _thread.start()
        return True


def start_campaign(params) -> bool:
    """Begin gathering + composing on a background thread."""
    if is_busy():
        return False
    def setup():
        _STATE.reset()
        _STATE.update(status="running", step="Starting…", source=params["source"])
    return _spawn(_pipeline, (params,), setup)


def start_send(records, senders, campaign_id) -> bool:
    """Send approved emails on a background thread."""
    if is_busy():
        return False
    def setup():
        with _STATE.lock:
            _STATE.stop_event = threading.Event()
            _STATE.status = "sending"
            _STATE.prepared = list(records)
            _STATE.campaign_id = campaign_id
    return _spawn(_send, (records, senders, campaign_id), setup)


def resume_send(records, senders, campaign_id) -> bool:
    """Resume sending remaining unsent emails on a background thread."""
    return start_send(records, senders, campaign_id)


def resume_campaign(campaign_id: int, senders: list[dict], ai_api_key: str = "",
                    offering: str = "", company_name: str = "",
                    start_ready: bool = False,
                    custom_subject: str | None = None,
                    custom_body: str | None = None) -> bool:
    """Resume a paused campaign, whether paused during scraping/composition or sending."""
    if is_busy():
        return False
    pc = get_paused_campaign(campaign_id)
    if not pc:
        return False

    records = pc.get("unsent_records", [])
    stage = pc.get("resume_stage") or ""
    saved = pc.get("resume_data") or {}
    if start_ready and stage != "leads_ready":
        return False

    if stage == "leads_ready" and not start_ready:
        with _thread_lock:
            if _thread is not None and _thread.is_alive():
                _thread.join(timeout=2)
                if _thread.is_alive():
                    return False
            _STATE.reset()
            _STATE.update(
                status="leads_ready", campaign_id=campaign_id, source="scraper",
                leads_found=len(records),
                step="Scraped contacts ready — choose what to do next",
                detail="No messages have been composed or sent.",
            )
        return True

    if stage == "review":
        if not records:
            return False
        def show_review():
            _STATE.reset()
            _STATE.update(status="awaiting_review", prepared=records,
                          campaign_id=campaign_id,
                          step=f"{len(records)} email(s) ready for your review")
        with _thread_lock:
            if _thread is not None and _thread.is_alive():
                _thread.join(timeout=2)
                if _thread.is_alive():
                    return False
            show_review()
        return True

    if stage == "sending" or (not stage and records and
                              "subject" in records[0] and "body" in records[0]):
        if not records:
            update_campaign_status(campaign_id, "complete")
            _STATE.update(status="done", campaign_id=campaign_id,
                          step="All approved emails were sent")
            return True
        return resume_send(records, senders, campaign_id)

    saved_params = saved.get("params") or {}
    source = saved_params.get("source")
    if source not in ("scraper", "list"):
        source = "list" if pc.get("business_type") == "list_import" else "scraper"
    if not stage:
        if source == "list":
            stage = "composing"
        else:
            stage = "scraping" if not records else "finding_emails"
    if stage == "leads_ready":
        stage = "composing"

    params = {
        **saved_params,
        "source": source,
        "campaign_id": campaign_id,
        "senders": senders,
        "ai_api_key": ai_api_key,
        "offering": offering or saved_params.get("offering") or pc.get("offering") or "",
        "company_name": company_name or saved_params.get("company_name") or pc.get("company_name") or "",
        "company_website": saved_params.get("company_website") or pc.get("company_website") or "",
        "business_type": saved_params.get("business_type") or pc.get("business_type") or "",
        "location": saved_params.get("location") or pc.get("location") or "",
        "max_scrolls": int(saved_params.get("max_scrolls") or 15),
        "custom_subject": (custom_subject if custom_subject is not None
                           else saved_params.get("custom_subject") or ""),
        "custom_body": (custom_body if custom_body is not None
                        else saved_params.get("custom_body") or ""),
        "skip_already_sent": saved_params.get("skip_already_sent", True),
        "resume_stage": stage,
        "resume_records": records,
        "contacts": records,
        "prepared_prefix": saved.get("prepared") or [],
        "next_index": saved.get("next_index") or 0,
    }
    def setup():
        _STATE.reset()
        _STATE.update(status="running", step="Resuming paused campaign…",
                      campaign_id=campaign_id, source=source)
    return _spawn(_pipeline, (params,), setup)
