"""
services/medicine_sync.py
──────────────────────────
Imports medicine data from WHO Essential Medicines List and OpenFDA
to pre-populate the product catalogue, avoiding manual data entry.

DATA SOURCES USED:
  1. WHO EML embedded seed data (the 24th EML 2025, curated subset)
     → No API key required; data is embedded in code as authoritative seed.
     → Used for accurate ATC codes, categories, and prescription flags.

  2. OpenFDA Drug Label API  (https://api.fda.gov/drug/label.json)
     → Free, no key required (rate-limited to 240 req/min).
     → Enriches catalogue with brand names, manufacturer, dosage details.
     → Optional API key raises limit to 1000 req/min.

STRATEGY:
  • Phase 1: Load WHO EML seed data into DB (idempotent – skip if openfda_id or
    name already exists).
  • Phase 2: For each WHO medicine, query OpenFDA to enrich with brand/manufacturer
    data (best-effort – failures are logged, not fatal).

RISKS MITIGATED:
  • Idempotency check before every insert → safe to run multiple times.
  • httpx timeout on every request → never hangs indefinitely.
  • Rate limit via asyncio.sleep between batches → respects OpenFDA limits.
  • All DB writes in a single transaction per batch → rolled back on error.
  • Controlled substances flagged from WHO data → dispenser is warned.
  • Input sanitisation: only expected fields extracted from API response.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from config import get_settings
from models.orm import Medicine

logger = logging.getLogger(__name__)
settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════════
# WHO EML 24th LIST (2025) – CURATED SEED DATA
# ══════════════════════════════════════════════════════════════════════════════
# This is a representative subset of the 24th WHO Essential Medicines List.
# Source: https://www.who.int/groups/expert-committee-on-selection-and-use-of-
#         essential-medicines/essential-medicines-lists
# Full integration → add more entries following the same structure.

WHO_EML_SEED: List[Dict[str, Any]] = [
    # ── Anaesthetics ────────────────────────────────────────────────────────
    {"name": "Ketamine", "generic_name": "Ketamine", "atc_code": "N01AX03",
     "dosage_form": "Injection", "strength": "50 mg/mL", "route": "Injection",
     "requires_prescription": True, "is_controlled": True, "who_eml_code": "1.1",
     "category": "Anaesthetics"},
    {"name": "Halothane", "generic_name": "Halothane", "atc_code": "N01AB01",
     "dosage_form": "Inhalation", "strength": "", "route": "Inhalation",
     "requires_prescription": True, "is_controlled": False, "who_eml_code": "1.1"},
    {"name": "Isoflurane", "generic_name": "Isoflurane", "atc_code": "N01AB06",
     "dosage_form": "Inhalation", "strength": "", "route": "Inhalation",
     "requires_prescription": True, "is_controlled": False, "who_eml_code": "1.1"},
    {"name": "Lidocaine", "generic_name": "Lidocaine", "atc_code": "N01BB02",
     "dosage_form": "Injection", "strength": "1%; 2%", "route": "Injection",
     "requires_prescription": True, "is_controlled": False, "who_eml_code": "1.2"},
    # ── Analgesics ──────────────────────────────────────────────────────────
    {"name": "Aspirin", "generic_name": "Acetylsalicylic acid", "atc_code": "N02BA01",
     "dosage_form": "Tablet", "strength": "100 mg; 300 mg; 500 mg", "route": "Oral",
     "requires_prescription": False, "is_controlled": False, "who_eml_code": "2.1"},
    {"name": "Ibuprofen", "generic_name": "Ibuprofen", "atc_code": "M01AE01",
     "dosage_form": "Tablet", "strength": "200 mg; 400 mg; 600 mg", "route": "Oral",
     "requires_prescription": False, "is_controlled": False, "who_eml_code": "2.1"},
    {"name": "Paracetamol", "generic_name": "Paracetamol (Acetaminophen)",
     "atc_code": "N02BE01",
     "dosage_form": "Tablet; Oral liquid", "strength": "500 mg; 120 mg/5 mL",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "2.1"},
    {"name": "Morphine", "generic_name": "Morphine", "atc_code": "N02AA01",
     "dosage_form": "Injection; Tablet", "strength": "10 mg/mL; 10 mg",
     "route": "Injection; Oral", "requires_prescription": True, "is_controlled": True,
     "who_eml_code": "2.2"},
    {"name": "Codeine", "generic_name": "Codeine", "atc_code": "N02AA59",
     "dosage_form": "Tablet", "strength": "30 mg", "route": "Oral",
     "requires_prescription": True, "is_controlled": True, "who_eml_code": "2.2"},
    # ── Antibiotics ─────────────────────────────────────────────────────────
    {"name": "Amoxicillin", "generic_name": "Amoxicillin", "atc_code": "J01CA04",
     "dosage_form": "Capsule; Oral liquid", "strength": "250 mg; 500 mg; 125 mg/5 mL",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.1"},
    {"name": "Amoxicillin + Clavulanic acid",
     "generic_name": "Amoxicillin + Clavulanic acid", "atc_code": "J01CR02",
     "dosage_form": "Tablet", "strength": "500 mg + 125 mg", "route": "Oral",
     "requires_prescription": True, "is_controlled": False, "who_eml_code": "6.2.1"},
    {"name": "Ciprofloxacin", "generic_name": "Ciprofloxacin", "atc_code": "J01MA02",
     "dosage_form": "Tablet; Injection", "strength": "250 mg; 500 mg; 200 mg/100 mL",
     "route": "Oral; Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.2"},
    {"name": "Metronidazole", "generic_name": "Metronidazole", "atc_code": "J01XD01",
     "dosage_form": "Tablet; Injection; Oral liquid",
     "strength": "200 mg; 400 mg; 500 mg/100 mL; 200 mg/5 mL",
     "route": "Oral; Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.2"},
    {"name": "Doxycycline", "generic_name": "Doxycycline", "atc_code": "J01AA02",
     "dosage_form": "Capsule; Tablet", "strength": "100 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.2"},
    {"name": "Azithromycin", "generic_name": "Azithromycin", "atc_code": "J01FA10",
     "dosage_form": "Tablet; Oral liquid", "strength": "250 mg; 500 mg; 200 mg/5 mL",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.2"},
    {"name": "Ceftriaxone", "generic_name": "Ceftriaxone", "atc_code": "J01DD04",
     "dosage_form": "Powder for injection", "strength": "250 mg; 1 g; 2 g",
     "route": "Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.1"},
    {"name": "Gentamicin", "generic_name": "Gentamicin", "atc_code": "J01GB03",
     "dosage_form": "Injection", "strength": "10 mg/mL; 40 mg/mL",
     "route": "Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.2"},
    # ── Antimalarials ────────────────────────────────────────────────────────
    {"name": "Artemether + Lumefantrine",
     "generic_name": "Artemether + Lumefantrine", "atc_code": "P01BF01",
     "dosage_form": "Tablet", "strength": "20 mg + 120 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.5.3.1"},
    {"name": "Artesunate", "generic_name": "Artesunate", "atc_code": "P01BE03",
     "dosage_form": "Injection; Tablet", "strength": "60 mg; 100 mg; 200 mg",
     "route": "Injection; Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.5.3.1"},
    {"name": "Chloroquine", "generic_name": "Chloroquine", "atc_code": "P01BA01",
     "dosage_form": "Tablet; Oral liquid", "strength": "150 mg base; 50 mg/5 mL",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.5.3"},
    # ── Antiretrovirals ──────────────────────────────────────────────────────
    {"name": "Tenofovir + Lamivudine + Dolutegravir",
     "generic_name": "Tenofovir disoproxil fumarate + Lamivudine + Dolutegravir",
     "atc_code": "J05AR19", "dosage_form": "Tablet",
     "strength": "300 mg + 300 mg + 50 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.4.2.1"},
    {"name": "Efavirenz + Emtricitabine + Tenofovir",
     "generic_name": "Efavirenz + Emtricitabine + Tenofovir disoproxil fumarate",
     "atc_code": "J05AR06", "dosage_form": "Tablet",
     "strength": "600 mg + 200 mg + 300 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.4.2.1"},
    # ── Cardiovascular ───────────────────────────────────────────────────────
    {"name": "Amlodipine", "generic_name": "Amlodipine", "atc_code": "C08CA01",
     "dosage_form": "Tablet", "strength": "5 mg; 10 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "12.3"},
    {"name": "Atenolol", "generic_name": "Atenolol", "atc_code": "C07AB03",
     "dosage_form": "Tablet", "strength": "50 mg; 100 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "12.3"},
    {"name": "Enalapril", "generic_name": "Enalapril", "atc_code": "C09AA02",
     "dosage_form": "Tablet", "strength": "2.5 mg; 5 mg; 10 mg; 20 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "12.3"},
    {"name": "Furosemide", "generic_name": "Furosemide", "atc_code": "C03CA01",
     "dosage_form": "Tablet; Injection", "strength": "40 mg; 10 mg/mL",
     "route": "Oral; Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "12.4"},
    {"name": "Simvastatin", "generic_name": "Simvastatin", "atc_code": "C10AA01",
     "dosage_form": "Tablet", "strength": "5 mg; 10 mg; 20 mg; 40 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "12.6"},
    # ── Diabetes ─────────────────────────────────────────────────────────────
    {"name": "Metformin", "generic_name": "Metformin hydrochloride", "atc_code": "A10BA02",
     "dosage_form": "Tablet", "strength": "500 mg; 850 mg; 1 g",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "18.5"},
    {"name": "Glibenclamide", "generic_name": "Glibenclamide", "atc_code": "A10BB01",
     "dosage_form": "Tablet", "strength": "2.5 mg; 5 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "18.5"},
    {"name": "Insulin (human, isophane)",
     "generic_name": "Insulin (human, isophane)", "atc_code": "A10AC01",
     "dosage_form": "Injection", "strength": "100 IU/mL",
     "route": "Subcutaneous", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "18.5"},
    # ── Respiratory ──────────────────────────────────────────────────────────
    {"name": "Salbutamol", "generic_name": "Salbutamol", "atc_code": "R03AC02",
     "dosage_form": "Inhaler; Tablet; Oral liquid",
     "strength": "100 mcg/dose; 2 mg; 4 mg; 2 mg/5 mL",
     "route": "Inhalation; Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "25.1"},
    {"name": "Prednisolone", "generic_name": "Prednisolone", "atc_code": "H02AB06",
     "dosage_form": "Tablet; Oral liquid", "strength": "5 mg; 1 mg/mL",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "3.1"},
    # ── Vitamins & Minerals ──────────────────────────────────────────────────
    {"name": "Ferrous sulfate", "generic_name": "Ferrous sulfate", "atc_code": "B03AA07",
     "dosage_form": "Tablet", "strength": "200 mg (60 mg elemental iron)",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "10.1"},
    {"name": "Folic acid", "generic_name": "Folic acid", "atc_code": "B03BB01",
     "dosage_form": "Tablet", "strength": "400 mcg; 5 mg",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "10.1"},
    {"name": "Zinc sulfate", "generic_name": "Zinc sulfate", "atc_code": "A12CB01",
     "dosage_form": "Tablet; Oral liquid", "strength": "20 mg; 10 mg/5 mL",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "10.5"},
    # ── Vaccines ─────────────────────────────────────────────────────────────
    {"name": "BCG Vaccine", "generic_name": "BCG Vaccine", "atc_code": "J07AN01",
     "dosage_form": "Injection (powder)", "strength": "0.1 mL/dose",
     "route": "Intradermal", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "19.3"},
    # ── Gastrointestinal ─────────────────────────────────────────────────────
    {"name": "Omeprazole", "generic_name": "Omeprazole", "atc_code": "A02BC01",
     "dosage_form": "Capsule; Tablet", "strength": "10 mg; 20 mg; 40 mg",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "17.1"},
    {"name": "Oral Rehydration Salts (ORS)",
     "generic_name": "Oral Rehydration Salts", "atc_code": "A07CA",
     "dosage_form": "Powder for oral solution",
     "strength": "Na 75 mmol/L, K 20 mmol/L, Cl 65 mmol/L",
     "route": "Oral", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "17.5.1"},
    {"name": "Metoclopramide", "generic_name": "Metoclopramide", "atc_code": "A03FA01",
     "dosage_form": "Tablet; Injection", "strength": "10 mg; 5 mg/mL",
     "route": "Oral; Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "17.2"},
    # ── Anti-TB ──────────────────────────────────────────────────────────────
    {"name": "Isoniazid + Rifampicin + Ethambutol + Pyrazinamide",
     "generic_name": "Isoniazid + Rifampicin + Ethambutol + Pyrazinamide",
     "atc_code": "J04AM05",
     "dosage_form": "Tablet (FDC)", "strength": "75 mg + 150 mg + 275 mg + 400 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "6.2.4"},
    # ── Mental Health ────────────────────────────────────────────────────────
    {"name": "Diazepam", "generic_name": "Diazepam", "atc_code": "N05BA01",
     "dosage_form": "Tablet; Injection; Rectal gel",
     "strength": "2 mg; 5 mg; 10 mg; 5 mg/mL",
     "route": "Oral; Injection; Rectal", "requires_prescription": True,
     "is_controlled": True, "who_eml_code": "24.3"},
    {"name": "Haloperidol", "generic_name": "Haloperidol", "atc_code": "N05AD01",
     "dosage_form": "Tablet; Injection", "strength": "2 mg; 5 mg; 5 mg/mL",
     "route": "Oral; Injection", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "24.1"},
    {"name": "Amitriptyline", "generic_name": "Amitriptyline", "atc_code": "N06AA09",
     "dosage_form": "Tablet", "strength": "10 mg; 25 mg; 50 mg; 75 mg",
     "route": "Oral", "requires_prescription": True, "is_controlled": False,
     "who_eml_code": "24.2.1"},
    # ── Dermatology ──────────────────────────────────────────────────────────
    {"name": "Hydrocortisone cream", "generic_name": "Hydrocortisone",
     "atc_code": "D07AA02",
     "dosage_form": "Cream", "strength": "1%",
     "route": "Topical", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "13.3"},
    {"name": "Clotrimazole", "generic_name": "Clotrimazole", "atc_code": "D01AC01",
     "dosage_form": "Cream; Vaginal tablet", "strength": "1%; 100 mg; 500 mg",
     "route": "Topical; Vaginal", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "13.1"},
    # ── Eye preparations ─────────────────────────────────────────────────────
    {"name": "Tetracycline eye ointment", "generic_name": "Tetracycline",
     "atc_code": "S01AA09",
     "dosage_form": "Eye ointment", "strength": "1%",
     "route": "Ophthalmic", "requires_prescription": False, "is_controlled": False,
     "who_eml_code": "21.1"},
]


# ══════════════════════════════════════════════════════════════════════════════
# OPENFDA ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_openfda_label(generic_name: str) -> Optional[Dict]:
    """
    Query OpenFDA Drug Label API for a medicine by generic name.
    Returns the first result dict or None on any error.

    Timeout: 10 s total (connect 5 s + read 5 s).
    """
    params: Dict[str, Any] = {
        "search": f'generic_name:"{generic_name}"',
        "limit": 1,
    }
    if settings.OPENFDA_API_KEY:
        params["api_key"] = settings.OPENFDA_API_KEY

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = await client.get(f"{settings.OPENFDA_BASE_URL}/label.json", params=params)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                return results[0] if results else None
            if resp.status_code == 404:
                return None   # No match – not an error
            logger.warning("OpenFDA returned %s for '%s'", resp.status_code, generic_name)
            return None
    except httpx.HTTPError as exc:
        logger.warning("OpenFDA request failed for '%s': %s", generic_name, exc)
        return None


def _extract_openfda_fields(label: Dict) -> Dict[str, Optional[str]]:
    """
    Extract safe, expected fields from an OpenFDA label response.
    Only named fields are read; unknown keys are ignored (no injection risk).
    """
    openfda = label.get("openfda", {})
    return {
        "brand_name": (openfda.get("brand_name") or [None])[0],
        "manufacturer": (openfda.get("manufacturer_name") or [None])[0],
        "openfda_id": (openfda.get("application_number") or
                       openfda.get("ndc") or [None])[0],
        "description": (label.get("description") or
                        label.get("indications_and_usage") or [None])[0],
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SYNC FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

async def sync_who_medicines(db: Session, enrich_with_openfda: bool = True) -> Dict[str, int]:
    """
    Import WHO EML seed data and optionally enrich with OpenFDA.

    Returns {"imported": N, "skipped": N, "errors": N}.
    """
    imported = skipped = errors = 0

    for entry in WHO_EML_SEED:
        try:
            # ── Idempotency check ─────────────────────────────────────────
            existing = (
                db.query(Medicine)
                .filter(Medicine.name == entry["name"])
                .first()
            )
            if existing:
                skipped += 1
                continue

            # ── Base record from WHO data ─────────────────────────────────
            med = Medicine(
                name=entry["name"],
                generic_name=entry.get("generic_name"),
                atc_code=entry.get("atc_code"),
                who_eml_code=entry.get("who_eml_code"),
                dosage_form=entry.get("dosage_form"),
                strength=entry.get("strength"),
                route=entry.get("route"),
                requires_prescription=entry.get("requires_prescription", False),
                is_controlled=entry.get("is_controlled", False),
                source="who_eml",
                unit_price=0,  # Pharmacy sets their own price
                reorder_level=10,
            )

            # ── Enrich from OpenFDA (best-effort) ─────────────────────────
            if enrich_with_openfda and entry.get("generic_name"):
                label = await _fetch_openfda_label(entry["generic_name"])
                if label:
                    fields = _extract_openfda_fields(label)
                    med.brand_name   = fields["brand_name"]
                    med.manufacturer = fields["manufacturer"]
                    med.description  = (fields["description"] or "")[:2000]  # truncate
                    if fields["openfda_id"]:
                        # Check for duplicate openfda_id before assigning
                        dup = db.query(Medicine).filter(
                            Medicine.openfda_id == fields["openfda_id"]
                        ).first()
                        if not dup:
                            med.openfda_id = fields["openfda_id"]
                            med.source = "who_eml+openfda"

                # Rate limiting – be a good API citizen
                await asyncio.sleep(0.25)

            db.add(med)
            db.commit()
            db.refresh(med)
            imported += 1
            logger.info("Imported: %s", med.name)

        except Exception as exc:
            db.rollback()
            errors += 1
            logger.error("Error importing '%s': %s", entry.get("name"), exc)

    logger.info("Medicine sync complete – imported=%d skipped=%d errors=%d",
                imported, skipped, errors)
    return {"imported": imported, "skipped": skipped, "errors": errors}