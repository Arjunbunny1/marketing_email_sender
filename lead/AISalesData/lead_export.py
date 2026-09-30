"""CSV export for scraped contacts ready for a future campaign."""

import csv
import io


def scraped_leads_csv(contacts: list[dict], business_type: str = "",
                      location: str = "") -> bytes:
    """Export contacts with email addresses in a List Campaign compatible CSV."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[
        "email", "first_name", "last_name", "company", "website",
        "business_type", "location",
    ])
    writer.writeheader()
    for contact in contacts:
        email = (contact.get("email") or "").strip()
        if not email:
            continue
        writer.writerow({
            "email": email,
            "first_name": contact.get("first_name") or "",
            "last_name": contact.get("last_name") or "",
            "company": contact.get("company_name") or "",
            "website": contact.get("website") or "",
            "business_type": business_type,
            "location": location,
        })
    return output.getvalue().encode("utf-8-sig")
