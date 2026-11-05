"""
Link identity records to candidate documents (Phase 5D P1-7).

Matches ``identity`` records against ``candidates`` by, in order:
  1. canonical candidate id (identity.canonical_candidate_id vs the
     candidate's canonical_candidate_id / candidate_id / id fields)
  2. case-insensitive email (email is a LINKING attribute only — a match
     links the records but never merges or overwrites identity data)

Classification per identity record:
  linked     — exactly one confident candidate match
  ambiguous  — more than one candidate matches (reported, never guessed)
  unlinked   — no candidate matches
  conflict   — candidate already carries a DIFFERENT canonical id
               (never overwritten — reported for manual remediation)

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
    "name": 1,
}


def _norm_email(value) -> str:
    return str(value or "").strip().lower()


def _candidate_canonical_ids(cand: dict) -> set:
    out = set()
    for key in ("canonical_candidate_id", "candidate_id", "id"):
        val = str(cand.get(key) or "").strip()
        if val:
            out.add(val)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write links (default: dry run, report only)",
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

    identities = list(db.identity.find({}, IDENTITY_FIELDS).limit(5000))
    print(f"Loaded {len(identities)} identity records.")

    # Candidate indexes for matching
    by_email = defaultdict(list)
    for cand in db.candidates.find({}, CANDIDATE_FIELDS):
        email = _norm_email(cand.get("email"))
        if email:
            by_email[email].append(cand)

    report = {"linked": [], "ambiguous": [], "unlinked": [], "conflict": [], "already": []}
    now = datetime.now(timezone.utc)

    for ident in identities:
        local_id = ident.get("sampada_local_id")
        canonical = str(ident.get("canonical_candidate_id") or "").strip()
        email = _norm_email(ident.get("email"))
        status = ident.get("canonical_id_status")

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
        cand_canonical = str(cand.get("canonical_candidate_id") or "").strip()
        if canonical and cand_canonical and cand_canonical != canonical:
            report["conflict"].append((local_id, str(cand["_id"]), cand_canonical, canonical))
            continue

        entry = (local_id, str(cand["_id"]), match_how, status)
        if args.apply:
            set_fields = {
                "identity_local_id": local_id,
                "identity_linked_at": now,
                "identity_link_source": match_how,
            }
            if canonical and not cand_canonical:
                set_fields["canonical_candidate_id"] = canonical
                set_fields["canonical_id_status"] = status or "VERIFIED"
            db.candidates.update_one({"_id": cand["_id"]}, {"$set": set_fields})
        report["linked"].append(entry)

    print("\n===== identity -> candidate linking report =====")
    print(f"mode      : {'APPLY' if args.apply else 'DRY RUN (pass --apply to write)'}")
    print(f"linked    : {len(report['linked'])}")
    print(f"ambiguous : {len(report['ambiguous'])}")
    print(f"unlinked  : {len(report['unlinked'])}")
    print(f"conflict  : {len(report['conflict'])}")

    for local_id, cand_id, how, status in report["linked"][:20]:
        print(f"  [linked]    {local_id} -> {cand_id} via {how} (canonical status={status})")
    for local_id, cand_ids, how in report["ambiguous"]:
        print(f"  [ambiguous] {local_id} matched {len(cand_ids)} candidates via {how}: {cand_ids}")
    for local_id, cand_id, existing, supplied in report["conflict"]:
        print(
            f"  [conflict]  {local_id} vs candidate {cand_id}: "
            f"candidate has '{existing}', identity has '{supplied}' — NOT overwritten"
        )
    for local_id in report["unlinked"][:20]:
        print(f"  [unlinked]  {local_id}")
    if len(report["unlinked"]) > 20:
        print(f"  ... and {len(report['unlinked']) - 20} more unlinked")

    print("\nRules: email is a linking attribute only; canonical ids are never")
    print("invented, overwritten, or merged by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
