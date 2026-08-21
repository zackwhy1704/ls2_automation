"""The trust gate must withhold auto-submit for a Sengkang WO that looks like mosquito/vector work.

Sengkang's Synergix Project Sites are "2000073 Pest control" and "2000130 Mosquito", and the driver
always picks 2000073 for a non-JBTC council. Every real SKTC WO seen so far is pest control, so that
default is right for them — but whether Sengkang ever sends mosquito work, and whether it belongs on
2000130, is unconfirmed. Rather than silently bill such a WO to the wrong project, it goes to
NEEDS_REVIEW.

The false-positive cases below matter as much as the positive one: a guard that fires on ordinary
rodent or pigeon work would push every SKTC WO to manual review and quietly kill the automation.
"""
from __future__ import annotations

from datetime import date

from src.models import LineItem, WOPayload
from src.validator import check_extraction_trust


def _payload(**over) -> WOPayload:
    base = dict(
        wo_po_number="WO-PO/000061116",
        town_council="SENGKANG TOWN COUNCIL",
        job_sheet_number="26449",
        service_location="227D to 228A Compassvale Drive",
        nature_of_work="Transport and administration charges for inspection of pest",
        job_date=date(2026, 7, 10),
        prepared_by="STEPHANIE LIM",
        gl_number="731-CP-SCPCOURT544227-9-721010-0000",
        quantity=1.0,
        unit_price=30.0,
        discount_percent=10.0,
        discount_amount=3.0,
        net_amount=27.0,
        gst_percent=9.0,
        grand_total=29.43,
        extraction_confidence=0.95,
        source_path="000061116.pdf",
    )
    base.update(over)
    return WOPayload(**base)


def _mosquito_concerns(concerns: list[str]) -> list[str]:
    return [c for c in concerns if "Project Site" in c]


def test_real_sktc_pigeon_wo_is_not_flagged():
    """WO-PO/000061116 verbatim: pigeon inspection. Pest control, so 2000073 is correct — the guard
    must stay silent or the automation stops working for the WOs it is meant to handle."""
    p = _payload(
        nature_of_work="SOR A027 - Inspection for pigeon activities. No pigeon activities found.",
    )
    assert _mosquito_concerns(check_extraction_trust(p)) == []


def test_sktc_rodent_breeding_habitats_is_not_flagged():
    """'breeding habitats' is standard rodent-treatment wording and must not read as mosquito work —
    which is exactly why 'breeding' is not one of the terms."""
    p = _payload(
        nature_of_work=(
            "RODENT TREATMENT - perform surveillance works for Rodent activities including "
            "inspection, locate and treatment to all breeding habitats of Rodent"
        ),
    )
    assert _mosquito_concerns(check_extraction_trust(p)) == []


def test_sktc_mosquito_wo_is_withheld():
    p = _payload(nature_of_work="Mosquito larviciding of stagnant water at drains")
    concerns = _mosquito_concerns(check_extraction_trust(p))
    assert concerns, "a Sengkang mosquito WO must not be auto-billed to 2000073"
    assert "2000130" in concerns[0]


def test_term_found_in_a_line_item_description_also_counts():
    """The giveaway is often in the line description, not nature_of_work."""
    p = _payload(
        nature_of_work="Vector work",
        line_items=[LineItem(description="Aedes surveillance and treatment",
                             quantity=1.0, unit_price=30.0, net_amount=27.0)],
    )
    assert _mosquito_concerns(check_extraction_trust(p))


def test_jbtc_mosquito_wo_is_not_flagged():
    """JBTC's project code comes from the job-sheet prefix, so the Sengkang ambiguity does not apply
    and a JBTC fogging/mosquito WO must not be pushed to review by this rule."""
    p = _payload(
        town_council="JALAN BESAR TOWN COUNCIL",
        nature_of_work="Mosquito fogging",
        net_amount=33.0,
        grand_total=35.97,
        unit_price=30.0,
    )
    assert _mosquito_concerns(check_extraction_trust(p)) == []
