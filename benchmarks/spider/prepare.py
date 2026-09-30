#!/usr/bin/env python3
"""Freeze a mechanical, held-out Spider 1.0 dev subset before testing IntentSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.artifacts import artifact_root  # noqa: E402

ARCHIVE = artifact_root() / "spider/spider_data.zip"
MANIFEST = HERE / "manifest.json"
SEED = 1701
SECOND_MANIFEST = HERE / "manifest-2.json"
SECOND_SEED = 2609
THIRD_MANIFEST = HERE / "manifest-3.json"
THIRD_SEED = 3917
FOURTH_MANIFEST = HERE / "manifest-4.json"
FOURTH_SEED = 5923
FIFTH_MANIFEST = HERE / "manifest-5.json"
FIFTH_SEED = 7919
SIXTH_MANIFEST = HERE / "manifest-6.json"
SIXTH_SEED = 104729
SEVENTH_MANIFEST = HERE / "manifest-7.json"
SEVENTH_SEED = 130363
COUNT = 20
FINAL_COUNT = 40
OFFICIAL_DRIVE_ID = "1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def plain_column(unit: object) -> bool:
    return (isinstance(unit, list) and len(unit) == 3 and
            unit[0] == 0 and isinstance(unit[1], int) and
            isinstance(unit[2], bool) and not unit[2])


def plain_value(unit: object) -> bool:
    return (isinstance(unit, list) and len(unit) == 3 and unit[0] == 0 and
            plain_column(unit[1]) and unit[2] is None)


def plain_condition(item: object) -> bool:
    # Spider comparison operators: =, >, <, >=, <=, !=. Values must be
    # literal data, never another column, nested query, or expression.
    return (isinstance(item, list) and len(item) == 5 and item[0] is False and
            item[1] in (2, 3, 4, 5, 6, 7) and plain_value(item[2]) and
            type(item[3]) in (int, float, str) and item[4] is None)


def eligible(case: dict) -> bool:
    """Conservative SQL-AST subset of the currently supported read envelope.

    No request text or IntentSQL result participates in this filter. The app
    supports more than this subset; this filter avoids ambiguous SQL shapes.
    """
    sql = case["sql"]
    source = sql["from"]
    if any(sql[key] is not None for key in ("intersect", "union", "except")):
        return False
    if source["conds"] or len(source["table_units"]) != 1:
        return False
    if source["table_units"][0][0] != "table_unit":
        return False
    distinct, fields = sql["select"]
    if len(fields) != 1 or fields[0][0] not in (0, 1, 2, 3, 4, 5):
        return False
    if not isinstance(distinct, bool) or not plain_value(fields[0][1]):
        return False
    if sql["where"] and (len(sql["where"]) != 1 or not plain_condition(sql["where"][0])):
        return False
    if sql["groupBy"] or sql["having"]:
        return False
    order = sql["orderBy"]
    if order and (len(order) != 2 or order[0] not in ("asc", "desc") or
                  len(order[1]) != 1 or not plain_value(order[1][0])):
        return False
    if sql["limit"] is not None and (type(sql["limit"]) is not int or
                                     not 1 <= sql["limit"] <= 5000):
        return False
    query = case["query"].strip()
    return query.upper().startswith("SELECT ") and ";" not in query.rstrip(";")


def selection_settings(sample: int) -> tuple[int, Path, set[int]]:
    settings = {
        1: (SEED, MANIFEST), 2: (SECOND_SEED, SECOND_MANIFEST),
        3: (THIRD_SEED, THIRD_MANIFEST), 4: (FOURTH_SEED, FOURTH_MANIFEST),
        5: (FIFTH_SEED, FIFTH_MANIFEST),
        6: (SIXTH_SEED, SIXTH_MANIFEST),
        7: (SEVENTH_SEED, SEVENTH_MANIFEST),
    }
    seed, manifest = settings[sample]
    prior = (MANIFEST, SECOND_MANIFEST, THIRD_MANIFEST, FOURTH_MANIFEST,
             FIFTH_MANIFEST, SIXTH_MANIFEST)[:sample - 1]
    excluded = {case["source_index"] for path in prior
                for case in json.loads(path.read_text(encoding="utf-8"))["cases"]}
    return seed, manifest, excluded


def freeze(archive: Path, manifest: Path, sample: int = 1) -> dict:
    seed, _, excluded = selection_settings(sample)
    count = FINAL_COUNT if sample == 7 else COUNT
    with zipfile.ZipFile(archive) as source:
        raw = source.read("spider_data/dev.json")
        dev = json.loads(raw)
        pool = [i for i, case in enumerate(dev) if eligible(case)]
        available = [i for i in pool if i not in excluded]
        if len(available) < count:
            raise ValueError(f"Only {len(available)} unseen eligible cases; need {count}")
        selected = sorted(random.Random(seed).sample(available, count))
        cases = []
        for index in selected:
            case = dev[index]
            db_id = case["db_id"]
            member = f"spider_data/database/{db_id}/{db_id}.sqlite"
            db_data = source.read(member)
            cases.append({
                "id": f"dev:{index:04d}", "source_index": index, "db_id": db_id,
                "question": case["question"], "gold_sql": case["query"],
                "gold_ast": case["sql"], "database_sha256": digest(db_data),
            })
    frozen = {
        "source": "https://yale-lily.github.io/spider",
        "official_drive_id": OFFICIAL_DRIVE_ID,
        "split": "dev.json", "seed": seed, "examined": len(dev),
        "eligible": len(pool), "selected": count,
        "archive_sha256": file_digest(archive), "dev_json_sha256": digest(raw),
        "cases": cases,
    }
    if sample > 1:
        prior = (MANIFEST, SECOND_MANIFEST, THIRD_MANIFEST, FOURTH_MANIFEST,
                 FIFTH_MANIFEST, SIXTH_MANIFEST)[:sample - 1]
        frozen["excluded_manifest_sha256s"] = [file_digest(path) for path in prior]
        frozen["excluded_source_indices"] = sorted(excluded)
    manifest.write_text(json.dumps(frozen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return frozen


def preflight(archive: Path, manifest: Path, sample: int = 1) -> dict:
    frozen = json.loads(manifest.read_text(encoding="utf-8"))
    seed, _, excluded = selection_settings(sample)
    count = FINAL_COUNT if sample == 7 else COUNT
    if frozen["seed"] != seed or frozen["official_drive_id"] != OFFICIAL_DRIVE_ID:
        raise ValueError("Frozen source or selection seed changed")
    if sample > 1:
        prior = (MANIFEST, SECOND_MANIFEST, THIRD_MANIFEST, FOURTH_MANIFEST,
                 FIFTH_MANIFEST, SIXTH_MANIFEST)[:sample - 1]
        # Accept the original sample-2 field while preserving its frozen file.
        hashes = frozen.get("excluded_manifest_sha256s")
        if sample == 2 and hashes is None:
            hashes = [frozen.get("excluded_first_manifest_sha256")]
        if (hashes != [file_digest(path) for path in prior] or
                frozen.get("excluded_source_indices") != sorted(excluded)):
            raise ValueError("Prior Spider sample exclusion changed")
    if frozen["archive_sha256"] != file_digest(archive):
        raise ValueError("Spider archive hash differs from frozen manifest")
    if len(frozen["cases"]) != count or frozen["selected"] != count:
        raise ValueError(f"Frozen selection must contain exactly {count} cases")
    with zipfile.ZipFile(archive) as source:
        raw = source.read("spider_data/dev.json")
        dev = json.loads(raw)
        pool = [i for i, case in enumerate(dev) if eligible(case)]
        expected = sorted(random.Random(seed).sample([i for i in pool if i not in excluded], count))
        actual = [case["source_index"] for case in frozen["cases"]]
        if digest(raw) != frozen["dev_json_sha256"] or len(dev) != frozen["examined"] or len(pool) != frozen["eligible"] or actual != expected:
            raise ValueError("Frozen selection does not match the official dev split and mechanical filter")
        for selected in frozen["cases"]:
            original = dev[selected["source_index"]]
            if (original["db_id"] != selected["db_id"] or
                original["question"] != selected["question"] or
                original["query"] != selected["gold_sql"] or
                original["sql"] != selected["gold_ast"]):
                raise ValueError(f"Frozen case changed: {selected['id']}")
            member = f"spider_data/database/{selected['db_id']}/{selected['db_id']}.sqlite"
            if digest(source.read(member)) != selected["database_sha256"]:
                raise ValueError(f"Database hash changed: {selected['id']}")
    print(f"Spider dev examined: {frozen['examined']}; mechanically eligible: {frozen['eligible']}; "
          f"excluded prior cases: {len(excluded)}; seed: {frozen['seed']}")
    print("Frozen selection: " + ", ".join(f"{c['id']}/{c['db_id']}" for c in frozen["cases"]))
    print("Selection was frozen before IntentSQL execution. Preflight made zero Jev calls.")
    return frozen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--sample", type=int, choices=(1, 2, 3, 4, 5, 6, 7), default=1)
    parser.add_argument("--freeze", action="store_true", help="create frozen manifest once; refuses overwrite")
    args = parser.parse_args()
    if args.manifest == MANIFEST:
        args.manifest = {1: MANIFEST, 2: SECOND_MANIFEST,
                         3: THIRD_MANIFEST, 4: FOURTH_MANIFEST,
                         5: FIFTH_MANIFEST, 6: SIXTH_MANIFEST,
                         7: SEVENTH_MANIFEST}[args.sample]
    if args.freeze:
        if args.manifest.exists():
            parser.error("manifest already exists; selection is immutable")
        freeze(args.archive, args.manifest, args.sample)
    preflight(args.archive, args.manifest, args.sample)


if __name__ == "__main__":
    main()
