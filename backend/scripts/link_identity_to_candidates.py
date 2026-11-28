"""
Link identity records to candidate documents (Phase 5D P1-7).

Matches ``identity`` records against ``candidates`` by, in order:
  1. canonical candidate id (identity.canonical_candidate_id vs the
     candidate's canonical_candidate_id / candidate_id / id fields)
  2. case-insensitive email (email is a LINKING attribute only — a match
     links the records but never merges or overwrites identity data)

Classification per identity record:
  linked     — exactly one confident candidate match
  already    — link already present and canonical id consistent (no write;
               makes re-runs idempotent and reportable)
  ambiguous  — more than one candidate matches (reported, never guessed)
  unlinked   — no candidate matches
  quarantined— identity is QUARANTINED (audit: never linked automatically)
  conflict   — candidate already carries a DIFFERENT canonical id across any
               of its id fields (canonical_candidate_id / candidate_id / id —
               never overwritten — reported for manual remediation)

Default is a DRY RUN that only prints the report. Pass --apply to write:
  candidate.canonical_candidate_id   (only when absent — never overwritten)
  candidate.canonical_id_status      (only when absent)
  candidate.identity_local_id        (reverse link, stamped with link metadata)

Usage (from the repository root or backend/):
    python backend/scripts/link_identity_to_candidates.py           # dry run
    python backend/scripts/link_identity_to_candidates.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Load backend/.env explicitly (same walk-up as the gateway).
_env_path = os.path.join(BACKEND_DIR, ".env")
if os.path.isfile(_env_path):
    load_dotenv(_env_path, override=False)
else:
    load_dotenv()

IDENTITY_FIELDS = {
    "sampada_local_id": 1,
    "canonical_candidate_id": 1,
    "canonical_id_status": 1,
    "email": 1,
    "institute_code": 1,
}
CANDIDATE_FIELDS = {
    "_id": 1,
    "email": 1,
    "id": 1,
    "candidate_id": 1,
    "canonical_candidate_id": 1,
    "identity_local_id": 1,
    "name": 1,
}


def _norm_email(value) -> str:
    return str(value or "").strip().lower()


def _candidate_canonical_ids(cand: dict) -> set:
    """All id-shaped values a candidate document already carries.

    Audit: conflict detection must consider the legacy ``candidate_id`` /
    ``id`` fields too — checking only ``canonical_candidate_id`` lets the
    script stamp a second, disagreeing id onto the same candidate.
    """
    out = set()
    for key in ("canonical_candidate_id", "candidate_id", "id"):
        val = str(cand.get(key) or "").strip()
        if val:
            out.add(val)
    return out


def _fresh_candidate(candidates, cand_id) -> Optional[dict]:
    """Re-read immediately before a write (the earlier scan may be stale)."""
    if hasattr(candidates, "find_one"):
        return candidates.find_one({"_id": cand_id})
    matches = list(candidates.find({"_id": cand_id}))
    return matches[0] if matches else None


def _link_is_safe(fresh: dict, local_id: str, canonical: str) -> tuple:
    """Compare-and-set guard: never take over a changed/foreign link.

    Audit: the initial email/canonical scan happens before this write. A
    concurrent run (or a second identity sharing the same email) can change
    the candidate in between. Re-reading and checking ownership prevents a
    duplicate-email match from overwriting another identity's canonical id
    or reverse link.
    """
    if fresh is None:
        return False, "candidate_disappeared"
    existing_link = str(fresh.get("identity_local_id") or "").strip()
    if existing_link and existing_link != str(local_id):
        return False, f"identity_link_taken:{existing_link}"
    if canonical and str(fresh.get("canonical_candidate_id") or "").strip():
        if str(fresh["canonical_candidate_id"]).strip() != canonical:
            return False, "canonical_id_conflict"
    if canonical:
        other_ids = _candidate_canonical_ids(fresh) - {canonical}
        if other_ids:
            return False, "legacy_id_conflict"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write links (default: dry run, report only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max identity records to process (0 = all; default streams "
        "the whole collection in batches — no silent 5000 truncation)",
    )
    args = parser.parse_args()

    uri = os.getenv("DATABASE_URL") or os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB_NAME", "bhiv_hr")
    if not uri:
        print("[ERROR] DATABASE_URL / MONGODB_URI not set — cannot connect.")
        return 2

    print(f"Connecting to MongoDB ({db_name}) ...")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db = client[db_name]
    client.admin.command("ping")

    # Candidate indexes for matching
    by_email = defaultdict(list)
    for cand in db.candidates.find({}, CANDIDATE_FIELDS):
        email = _norm_email(cand.get("email"))
        if email:
            by_email[email].append(cand)

    report = {
        "linked": [],
        "already": [],
        "ambiguous": [],
        "unlinked": [],
        "conflict": [],
        "quarantined": [],
    }
    now = datetime.now(timezone.utc)
    identity_count = 0

    # Audit: stream in batches (no .limit(5000) silent truncation).
    cursor = db.identity.find({}, IDENTITY_FIELDS).sort("_id", 1).batch_size(500)
    for ident in cursor:
        identity_count += 1
        if args.limit and identity_count > args.limit:
            identity_count -= 1
            break
        local_id = ident.get("sampada_local_id")
        canonical = str(ident.get("canonical_candidate_id") or "").strip()
        email = _norm_email(ident.get("email"))
        status = ident.get("canonical_id_status")

        # Audit: quarantined identities are NEVER linked automatically.
        if str(status or "").upper() == "QUARANTINED":
            report["quarantined"].append(local_id)
            continue

        matches = []
        match_how = None
        if canonical:
            for cand in db.candidates.find(
                {
                    "$or": [
                        {"canonical_candidate_id": canonical},
                        {"candidate_id": canonical},
                        {"id": canonical},
                    ]
                },
                CANDIDATE_FIELDS,
            ):
                matches.append(cand)
            if matches:
                match_how = "canonical_id"
        if not matches and email:
            matches = list(by_email.get(email, []))
            if matches:
                match_how = "email"

        if not matches:
            report["unlinked"].append(local_id)
            continue
        if len(matches) > 1:
            report["ambiguous"].append(
                (local_id, [str(m["_id"]) for m in matches], match_how)
            )
            continue

        cand = matches[0]
        cand_ids = _candidate_canonical_ids(cand)
        cand_canonical = str(cand.get("canonical_candidate_id") or "").strip()
        if canonical and cand_ids and canonical not in cand_ids:
            # Candidate already carries a DIFFERENT id (any field) — report.
            report["conflict"].append(
                (
                    local_id,
                    str(cand["_id"]),
                    ", ".join(sorted(cand_ids)),
                    canonical,
                )
            )
            continue

        # Audit (duplicate-email overwrite): re-read under the write path and
        # compare against the authoritative current document before stamping.
        fresh = _fresh_candidate(db.candidates, cand["_id"])
        safe, reason = _link_is_safe(fresh, local_id, canonical)
        if not safe:
            report["conflict"].append(
                (local_id, str(cand["_id"]), reason, canonical or "-")
            )
            continue
        fresh_canonical = str(fresh.get("canonical_candidate_id") or "").strip()

        entry = (local_id, str(cand["_id"]), match_how, status)
        already_linked = str(fresh.get("identity_local_id") or "") == str(local_id)
        canonical_stamped = bool(fresh_canonical)
        needs_write = (not already_linked) or (bool(canonical) and not canonical_stamped)
        if not needs_write:
            # Idempotent re-run: link + canonical already consistent.
            report["already"].append(entry)
            continue

        if args.apply:
            set_fields = {
                "identity_local_id": local_id,
                "identity_linked_at": now,
                "identity_link_source": match_how,
            }
            if canonical and not fresh_canonical:
                set_fields["canonical_candidate_id"] = canonical
                set_fields["canonical_id_status"] = status or "VERIFIED"
            # Compare-and-set: the update only applies if the freshly-read
            # ownership/id state is still unchanged. A concurrent writer wins
            # by making our filter match nothing; we never overwrite it.
            write_filter = {
                "_id": cand["_id"],
                "identity_local_id": {"$in": [None, "", local_id]},
            }
            if canonical and not fresh_canonical:
                write_filter["canonical_candidate_id"] = {
                    "$in": [None, "", canonical]
                }
            result = db.candidates.update_one(write_filter, {"$set": set_fields})
            matched = getattr(result, "matched_count", 1)
            if matched == 0:
                report["conflict"].append(
                    (local_id, str(cand["_id"]), "cas_write_lost", canonical or "-")
                )
                continue
        report["linked"].append(entry)

    print("\n===== identity -> candidate linking report =====")
    print(f"mode       : {'APPLY' if args.apply else 'DRY RUN (pass --apply to write)'}")
    print(f"processed  : {identity_count}")
    print(f"linked     : {len(report['linked'])}")
    print(f"already    : {len(report['already'])}")
    print(f"ambiguous  : {len(report['ambiguous'])}")
    print(f"unlinked   : {len(report['unlinked'])}")
    print(f"conflict   : {len(report['conflict'])}")
    print(f"quarantined: {len(report['quarantined'])}")

    for local_id, cand_id, how, status in report["linked"][:20]:
        print(f"  [linked]     {local_id} -> {cand_id} via {how} (canonical status={status})")
    for local_id, cand_id, how, status in report["already"][:20]:
        print(f"  [already]    {local_id} -> {cand_id} via {how} (no write needed)")
    for local_id, cand_ids, how in report["ambiguous"]:
        print(f"  [ambiguous]  {local_id} matched {len(cand_ids)} candidates via {how}: {cand_ids}")
    for local_id, cand_id, existing, supplied in report["conflict"]:
        print(
            f"  [conflict]   {local_id} vs candidate {cand_id}: "
            f"candidate has '{existing}', identity has '{supplied}' — NOT overwritten"
        )
    for local_id in report["quarantined"][:20]:
        print(f"  [quarantined] {local_id} — skipped (identity.ambiguous.v1 open)")
    for local_id in report["unlinked"][:20]:
        print(f"  [unlinked]   {local_id}")
    if len(report["unlinked"]) > 20:
        print(f"  ... and {len(report['unlinked']) - 20} more unlinked")

    print("\nRules: email is a linking attribute only; canonical ids are never")
    print("invented, overwritten, or merged by this script; QUARANTINED")
    print("identities are never linked automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
