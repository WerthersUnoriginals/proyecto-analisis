"""Pure, reproducible two-level comparator for the C contract.

Level 1 compares the nine fundamental inputs independently from provenance.
Level 2 gives both inputs the same frozen complements and calls the production
``build_c_score`` function. This module performs no acquisition or persistence.
"""

from __future__ import annotations

import copy
import json
import math
from collections import Counter
from numbers import Real
from typing import Optional

from c_score_v1 import build_c_score
from database.c_dual_run_contract import (
    COMPARISON_POLICY,
    CONTRACT_VERSION,
    FUNDAMENTAL_INPUT_KEYS,
    TOLERANCE_CONTRACT_VERSION,
)


_FIELD_POLICIES = {
    "latest_eps_yoy_pct": "yoy_pct",
    "previous_eps_yoy_pct": "yoy_pct",
    "eps_acceleration_pp": "acceleration_pp",
    "latest_revenue_yoy_pct": "yoy_pct",
    "previous_revenue_yoy_pct": "yoy_pct",
    "revenue_acceleration_pp": "acceleration_pp",
    "latest_eps": "eps",
}


def _difference_values(legacy: Real, new: Real) -> tuple[float, float]:
    absolute = abs(float(legacy) - float(new))
    scale = max(abs(float(legacy)), abs(float(new)))
    return absolute, absolute / scale if scale else 0.0


def _numeric_comparison(field: str, legacy: Real, new: Real, policy_name: str) -> dict:
    policy = COMPARISON_POLICY[policy_name]
    absolute, relative = _difference_values(legacy, new)
    finite = math.isfinite(float(legacy)) and math.isfinite(float(new))
    if legacy == new and finite:
        status = "EXACT"
        equivalent = True
    elif finite and (
        absolute <= policy["abs_tol"] or relative <= policy["rel_tol"]
    ):
        status = "NUMERIC_EQUIVALENT"
        equivalent = True
    else:
        status = "NUMERIC_DIFFERENCE"
        equivalent = False
    return {
        "field": field,
        "legacy": legacy,
        "new": new,
        "status": status,
        "equivalent": equivalent,
        "absolute_difference": absolute,
        "relative_difference": relative,
        "tolerance_used": policy_name,
    }


def _exact_comparison(field: str, legacy, new, difference_status: str = "SEMANTIC_DIFFERENCE") -> dict:
    exact = type(legacy) is type(new) and legacy == new
    return {
        "field": field,
        "legacy": legacy,
        "new": new,
        "status": "EXACT" if exact else difference_status,
        "equivalent": exact,
    }


def _missing_comparison(field: str, legacy_present: bool, legacy, new_present: bool, new) -> dict:
    status = "MISSING_LEGACY" if not legacy_present else "MISSING_NEW"
    return {
        "field": field,
        "legacy": legacy,
        "new": new,
        "legacy_present": legacy_present,
        "new_present": new_present,
        "status": status,
        "equivalent": False,
    }


def _scalar_comparison(field: str, legacy_present: bool, legacy, new_present: bool, new) -> dict:
    if not legacy_present or not new_present:
        return _missing_comparison(field, legacy_present, legacy, new_present, new)
    if legacy is None or new is None:
        return _exact_comparison(field, legacy, new)
    if field in _FIELD_POLICIES:
        if (
            isinstance(legacy, Real) and not isinstance(legacy, bool)
            and isinstance(new, Real) and not isinstance(new, bool)
        ):
            return _numeric_comparison(field, legacy, new, _FIELD_POLICIES[field])
        return _exact_comparison(field, legacy, new)
    return _exact_comparison(field, legacy, new)


def _history_record_comparison(path: str, legacy_record, new_record) -> tuple[dict, list[dict]]:
    """Compare one date-aligned history pair using the central tolerance policy."""
    if not isinstance(legacy_record, dict) or not isinstance(new_record, dict):
        comparison = _exact_comparison(path, legacy_record, new_record)
        return comparison, [] if comparison["equivalent"] else [comparison]
    if "date" not in legacy_record or "date" not in new_record:
        date = _missing_comparison(
            f"{path}.date", "date" in legacy_record, legacy_record.get("date"),
            "date" in new_record, new_record.get("date"),
        )
    else:
        date = _exact_comparison(f"{path}.date", legacy_record["date"], new_record["date"], "DATE_DIFFERENCE")
    if "value" not in legacy_record or "value" not in new_record:
        value = _missing_comparison(
            f"{path}.value", "value" in legacy_record, legacy_record.get("value"),
            "value" in new_record, new_record.get("value"),
        )
    elif (
        isinstance(legacy_record["value"], Real) and not isinstance(legacy_record["value"], bool)
        and isinstance(new_record["value"], Real) and not isinstance(new_record["value"], bool)
    ):
        value = _numeric_comparison(f"{path}.value", legacy_record["value"], new_record["value"], "yoy_pct")
    else:
        value = _exact_comparison(f"{path}.value", legacy_record["value"], new_record["value"])
    equivalent = date["equivalent"] and value["equivalent"]
    status = "EXACT" if date["status"] == "EXACT" and value["status"] == "EXACT" else (
        "NUMERIC_EQUIVALENT" if date["status"] == "EXACT" and value["equivalent"] else "SEMANTIC_DIFFERENCE"
    )
    record = {"path": path, "status": status, "equivalent": equivalent, "date": date, "value": value}
    return record, [item for item in (date, value) if item["status"] != "EXACT"]


def _history_by_date(items: list) -> tuple[dict, list]:
    by_date = {}
    duplicates = []
    for item in items:
        key = item.get("date") if isinstance(item, dict) else None
        if key in by_date:
            duplicates.append(key)
        else:
            by_date[key] = item
    return by_date, duplicates


def _score_relevant_history(legacy: list, new: list) -> tuple[dict, list[dict]]:
    legacy_tail, new_tail = legacy[-4:], new[-4:]
    comparison, differences = _series_comparison_core(legacy_tail, new_tail, align_by_date=False)
    comparison.update({"legacy_count": len(legacy_tail), "persisted_count": len(new_tail)})
    return comparison, differences


def _series_comparison_core(legacy: list, new: list, *, align_by_date: bool) -> tuple[dict, list[dict]]:
    field = "eps_yoy_pct"
    differences = []
    records = []
    if align_by_date:
        legacy_by_date, legacy_duplicates = _history_by_date(legacy)
        new_by_date, new_duplicates = _history_by_date(new)
        common_dates = sorted(set(legacy_by_date) & set(new_by_date), key=lambda value: str(value))
        legacy_only = sorted(set(legacy_by_date) - set(new_by_date), key=lambda value: str(value))
        new_only = sorted(set(new_by_date) - set(legacy_by_date), key=lambda value: str(value))
        for key in common_dates:
            record, record_differences = _history_record_comparison(
                f"{field}[date={key}]", legacy_by_date[key], new_by_date[key],
            )
            records.append(record)
            differences.extend(record_differences)
        for key in legacy_only:
            item = {"field": f"{field}[date={key}]", "legacy": legacy_by_date[key], "new": None,
                    "status": "EXCLUSIVE_LEGACY", "equivalent": False}
            differences.append(item)
        for key in new_only:
            item = {"field": f"{field}[date={key}]", "legacy": None, "new": new_by_date[key],
                    "status": "EXCLUSIVE_PERSISTED", "equivalent": False}
            differences.append(item)
        order_changed = [item.get("date") for item in legacy if isinstance(item, dict)] != [item.get("date") for item in new if isinstance(item, dict)]
        diagnostic = {
            "common_count": len(common_dates),
            "legacy_only_dates": legacy_only,
            "persisted_only_dates": new_only,
            "common_differences": [item for item in differences if "date=" in item.get("field", "") and item.get("status") not in {"EXCLUSIVE_LEGACY", "EXCLUSIVE_PERSISTED"}],
            "duplicate_dates": {"legacy": legacy_duplicates, "new": new_duplicates},
            "order_changed": order_changed,
            "equivalent": all(item["equivalent"] for item in differences)
            and not legacy_duplicates and not new_duplicates and not order_changed,
        }
        status = "EXACT" if diagnostic["equivalent"] and not differences else (
            "NUMERIC_EQUIVALENT" if diagnostic["equivalent"] else "SEMANTIC_DIFFERENCE"
        )
        return {"field": field, "legacy": legacy, "new": new, "status": status,
                "equivalent": diagnostic["equivalent"], "records": records, "full_history_diagnostic": diagnostic}, differences

    for index in range(max(len(legacy), len(new))):
        if index >= len(legacy) or index >= len(new):
            item = {"field": f"{field}[{index}]", "legacy": legacy[index] if index < len(legacy) else None,
                    "new": new[index] if index < len(new) else None,
                    "status": "EXCLUSIVE_LEGACY" if index < len(legacy) else "EXCLUSIVE_PERSISTED", "equivalent": False}
            differences.append(item)
            continue
        record, record_differences = _history_record_comparison(f"{field}[{index}]", legacy[index], new[index])
        record["index"] = index
        records.append(record)
        differences.extend(record_differences)
    equivalent = all(item["equivalent"] for item in differences)
    status = "EXACT" if equivalent and legacy == new else "NUMERIC_EQUIVALENT" if equivalent else "SEMANTIC_DIFFERENCE"
    return {"field": field, "legacy": legacy, "new": new, "status": status, "equivalent": equivalent, "records": records}, differences


def _series_comparison(legacy_present: bool, legacy, new_present: bool, new) -> tuple[dict, list[dict]]:
    field = "eps_yoy_pct"
    if not legacy_present or not new_present:
        comparison = _missing_comparison(field, legacy_present, legacy, new_present, new)
        return comparison, [comparison]
    if not isinstance(legacy, list) or not isinstance(new, list):
        comparison = _exact_comparison(field, legacy, new)
        return comparison, [] if comparison["equivalent"] else [comparison]

    full, differences = _series_comparison_core(legacy, new, align_by_date=True)
    score_relevant, _score_differences = _score_relevant_history(legacy, new)
    # Preserve the legacy positional record shape only for equal-length inputs;
    # unequal histories must never manufacture index-shift cascades.
    if len(legacy) == len(new):
        positional, positional_differences = _series_comparison_core(legacy, new, align_by_date=False)
        full["records"] = positional["records"]
        # Retain explicit positional date/value evidence for equal-length
        # histories whose identities changed; this is compatibility metadata,
        # not the alignment strategy used for unequal histories.
        if [item.get("date") for item in legacy if isinstance(item, dict)] != [item.get("date") for item in new if isinstance(item, dict)]:
            differences.extend(positional_differences)
        legacy_serialized = Counter(json.dumps(item, sort_keys=True) for item in legacy)
        new_serialized = Counter(json.dumps(item, sort_keys=True) for item in new)
        if legacy_serialized == new_serialized and legacy != new:
            differences.append({
                "field": field,
                "legacy": legacy,
                "new": new,
                "status": "SEMANTIC_DIFFERENCE",
                "reason": "ORDER_DIFFERENCE",
                "equivalent": False,
            })
    full["score_relevant_history"] = score_relevant
    return full, differences


def _provenance_comparison(legacy: Optional[dict], new: Optional[dict]) -> dict:
    expected_fields = set(FUNDAMENTAL_INPUT_KEYS)
    missing_fields = [
        field
        for field in FUNDAMENTAL_INPUT_KEYS
        if legacy is None or new is None or field not in legacy or field not in new
    ]
    if legacy is None or new is None:
        return {
            "status": "NOT_COMPARABLE",
            "equivalent": None,
            "legacy_available": legacy is not None,
            "new_available": new is not None,
            "missing_fields": missing_fields,
            "differences": [],
        }
    if not expected_fields.issubset(legacy) or not expected_fields.issubset(new):
        return {
            "status": "NOT_COMPARABLE",
            "equivalent": None,
            "legacy_available": True,
            "new_available": True,
            "missing_fields": missing_fields,
            "differences": [],
        }

    differences = []
    for field in sorted(set(legacy) | set(new)):
        legacy_field = legacy.get(field)
        new_field = new.get(field)
        if field not in legacy or field not in new:
            differences.append(_missing_comparison(
                field,
                field in legacy,
                legacy_field,
                field in new,
                new_field,
            ))
            continue
        if not isinstance(legacy_field, dict) or not isinstance(new_field, dict):
            comparison = _exact_comparison(field, legacy_field, new_field)
            if not comparison["equivalent"]:
                differences.append(comparison)
            continue
        for key in sorted(set(legacy_field) | set(new_field)):
            path = f"{field}.{key}"
            if key not in legacy_field or key not in new_field:
                differences.append(_missing_comparison(
                    path,
                    key in legacy_field,
                    legacy_field.get(key),
                    key in new_field,
                    new_field.get(key),
                ))
                continue
            status = (
                "SOURCE_DIFFERENCE"
                if key in {"source", "source_variant"}
                else "DATE_DIFFERENCE"
                if "date" in key
                else "SEMANTIC_DIFFERENCE"
            )
            comparison = _exact_comparison(
                path,
                legacy_field[key],
                new_field[key],
                status,
            )
            if not comparison["equivalent"]:
                differences.append(comparison)
    return {
        "status": "EQUIVALENT" if not differences else "DIFFERENT",
        "equivalent": not differences,
        "legacy_available": True,
        "new_available": True,
        "missing_fields": [],
        "differences": differences,
    }


def compare_fundamental_contract(
    legacy_fundamentals: dict,
    new_fundamentals: dict,
    *,
    legacy_provenance: Optional[dict] = None,
    new_provenance: Optional[dict] = None,
) -> dict:
    """Compare the nine Task 10A inputs without scoring them."""
    fields = {}
    differences = []
    for field in FUNDAMENTAL_INPUT_KEYS:
        legacy_present = field in legacy_fundamentals
        new_present = field in new_fundamentals
        if field == "eps_yoy_pct":
            comparison, field_differences = _series_comparison(
                legacy_present,
                legacy_fundamentals.get(field),
                new_present,
                new_fundamentals.get(field),
            )
            fields[field] = comparison
            differences.extend(field_differences)
            continue
        comparison = _scalar_comparison(
            field,
            legacy_present,
            legacy_fundamentals.get(field),
            new_present,
            new_fundamentals.get(field),
        )
        fields[field] = comparison
        if comparison["status"] != "EXACT":
            differences.append(comparison)

    value_equivalent = all(item["equivalent"] for item in fields.values())
    provenance = _provenance_comparison(legacy_provenance, new_provenance)
    provenance_equivalent = provenance["equivalent"]
    equivalent = value_equivalent and provenance_equivalent is True
    equivalence_status = (
        "EQUIVALENT"
        if equivalent
        else "VALUES_EQUIVALENT_PROVENANCE_NOT_COMPARABLE"
        if value_equivalent and provenance_equivalent is None
        else "VALUE_DIFFERENCE"
        if not value_equivalent
        else "PROVENANCE_DIFFERENCE"
    )
    return {
        "equivalent": equivalent,
        "equivalence_status": equivalence_status,
        "value_equivalent": value_equivalent,
        "provenance_equivalent": provenance_equivalent,
        "fields": fields,
        "differences": differences,
        "provenance": provenance,
    }


def _append_score_comparison(differences: list[dict], comparison: dict) -> bool:
    if comparison["status"] != "EXACT":
        differences.append(comparison)
    return comparison["equivalent"]


def _compare_components(legacy: dict, new: dict) -> tuple[bool, list[dict]]:
    equivalent = True
    differences = []
    for component in sorted(set(legacy) | set(new)):
        if component not in legacy or component not in new:
            missing = _missing_comparison(
                f"components.{component}",
                component in legacy,
                legacy.get(component),
                component in new,
                new.get(component),
            )
            missing["component"] = component
            missing["property"] = None
            differences.append(missing)
            equivalent = False
            continue
        for key in sorted(set(legacy[component]) | set(new[component])):
            path = f"components.{component}.{key}"
            if key not in legacy[component] or key not in new[component]:
                comparison = _missing_comparison(
                    path,
                    key in legacy[component],
                    legacy[component].get(key),
                    key in new[component],
                    new[component].get(key),
                )
            elif key == "points" and legacy[component][key] is not None and new[component][key] is not None:
                comparison = _numeric_comparison(
                    path,
                    legacy[component][key],
                    new[component][key],
                    "component_points",
                )
            else:
                comparison = _exact_comparison(path, legacy[component][key], new[component][key])
            if comparison["status"] != "EXACT":
                comparison["component"] = component
                comparison["property"] = key
                differences.append(comparison)
            equivalent = equivalent and comparison["equivalent"]
    return equivalent, differences


def _compare_small_base(legacy: dict, new: dict, differences: list[dict]) -> bool:
    equivalent = True
    for key in sorted(set(legacy) | set(new)):
        path = f"small_base.{key}"
        if key not in legacy or key not in new:
            comparison = _missing_comparison(
                path,
                key in legacy,
                legacy.get(key),
                key in new,
                new.get(key),
            )
        elif key == "inferred_previous_eps" and legacy[key] is not None and new[key] is not None:
            comparison = _numeric_comparison(path, legacy[key], new[key], "eps")
        elif key == "score_penalty" and legacy[key] is not None and new[key] is not None:
            comparison = _numeric_comparison(path, legacy[key], new[key], "component_points")
        else:
            comparison = _exact_comparison(path, legacy[key], new[key])
        equivalent = equivalent and _append_score_comparison(differences, comparison)
    return equivalent


def _compare_score_results(legacy: dict, new: dict) -> dict:
    legacy_score = legacy.get("c_score_v1", {})
    new_score = new.get("c_score_v1", {})
    differences = []
    equivalent = True

    policies = {
        "raw_points": "raw_points",
        "available_points": None,
        "normalized_score": None,
    }
    specialized = {
        "components", "diagnostic", "small_base",
        "raw_points", "available_points", "normalized_score",
    }
    for key, policy in policies.items():
        if key not in legacy_score or key not in new_score:
            comparison = _missing_comparison(
                key,
                key in legacy_score,
                legacy_score.get(key),
                key in new_score,
                new_score.get(key),
            )
        elif policy and legacy_score[key] is not None and new_score[key] is not None:
            comparison = _numeric_comparison(key, legacy_score[key], new_score[key], policy)
        else:
            comparison = _exact_comparison(key, legacy_score[key], new_score[key])
        equivalent = equivalent and _append_score_comparison(differences, comparison)

    components_equivalent, component_differences = _compare_components(
        legacy_score.get("components", {}),
        new_score.get("components", {}),
    )
    equivalent = equivalent and components_equivalent

    legacy_diagnostic = legacy_score.get("diagnostic", [])
    new_diagnostic = new_score.get("diagnostic", [])
    diagnostic_exact = type(legacy_diagnostic) is type(new_diagnostic) and legacy_diagnostic == new_diagnostic
    diagnostic_differences = [] if diagnostic_exact else {
        "legacy": legacy_diagnostic,
        "new": new_diagnostic,
        "missing": sorted(set(legacy_diagnostic) - set(new_diagnostic)),
        "extra": sorted(set(new_diagnostic) - set(legacy_diagnostic)),
        "order_changed": Counter(legacy_diagnostic) == Counter(new_diagnostic),
    }
    if not diagnostic_exact:
        differences.append({
            "field": "diagnostic",
            "legacy": legacy_diagnostic,
            "new": new_diagnostic,
            "status": "SEMANTIC_DIFFERENCE",
            "equivalent": False,
        })
        equivalent = False

    equivalent = _compare_small_base(
        legacy_score.get("small_base", {}),
        new_score.get("small_base", {}),
        differences,
    ) and equivalent

    for key in sorted((set(legacy_score) | set(new_score)) - specialized):
        if key not in legacy_score or key not in new_score:
            comparison = _missing_comparison(
                key,
                key in legacy_score,
                legacy_score.get(key),
                key in new_score,
                new_score.get(key),
            )
        else:
            comparison = _exact_comparison(key, legacy_score[key], new_score[key])
        equivalent = equivalent and _append_score_comparison(differences, comparison)

    legacy_flags = legacy.get("c_flags", [])
    new_flags = new.get("c_flags", [])
    flags_missing = sorted(set(legacy_flags) - set(new_flags))
    flags_extra = sorted(set(new_flags) - set(legacy_flags))
    flags_equivalent = not flags_missing and not flags_extra
    equivalent = equivalent and flags_equivalent
    if not flags_equivalent:
        differences.append({
            "field": "c_flags",
            "legacy": legacy_flags,
            "new": new_flags,
            "status": "SEMANTIC_DIFFERENCE",
            "equivalent": False,
        })

    classic = _exact_comparison(
        "c_classic",
        legacy.get("c_classic"),
        new.get("c_classic"),
    )
    equivalent = equivalent and _append_score_comparison(differences, classic)
    return {
        "equivalent": equivalent,
        "legacy": legacy,
        "new": new,
        "component_differences": component_differences,
        "flags_missing": flags_missing,
        "flags_extra": flags_extra,
        "diagnostic_differences": diagnostic_differences,
        "differences": differences,
    }


def _shared_complements(fixture: dict) -> dict:
    complements = fixture.get("shared_complements", {})
    if complements.get("shared_for_score_isolation") is not True:
        raise ValueError("shared_for_score_isolation must be true")
    if complements.get("independently_reconstructed_by_new_architecture") is not False:
        raise ValueError("independently_reconstructed_by_new_architecture must be false")
    values = complements.get("values", {})
    required = {"data_integrity", "split_integrity_status"}
    if set(values) != required:
        raise ValueError("shared complement values must contain exactly data_integrity and split_integrity_status")
    return copy.deepcopy(complements)


def compare_score_equivalence(
    legacy_fundamentals: dict,
    new_fundamentals: dict,
    fixture: dict,
) -> dict:
    """Compare C outputs after injecting one shared complement contract."""
    complements = _shared_complements(fixture)
    legacy_report = copy.deepcopy(legacy_fundamentals)
    new_report = copy.deepcopy(new_fundamentals)
    legacy_report.update(copy.deepcopy(complements["values"]))
    new_report.update(copy.deepcopy(complements["values"]))
    errors = []
    scores = {}
    for side, report in (("legacy", legacy_report), ("new", new_report)):
        try:
            scores[side] = build_c_score(report)
        except (TypeError, ValueError, ArithmeticError) as exc:
            scores[side] = None
            errors.append({
                "side": side,
                "type": type(exc).__name__,
                "message": str(exc),
            })
    if errors:
        result = {
            "equivalent": False,
            "status": "SCORE_EXECUTION_ERROR",
            "legacy": scores["legacy"],
            "new": scores["new"],
            "component_differences": [],
            "flags_missing": [],
            "flags_extra": [],
            "diagnostic_differences": [],
            "differences": [],
            "errors": errors,
        }
    else:
        result = _compare_score_results(scores["legacy"], scores["new"])
        result["status"] = "EQUIVALENT" if result["equivalent"] else "DIFFERENT"
        result["errors"] = []
    result["shared_complements"] = complements
    return result


def compare_c_dual_run(
    legacy_fundamentals: dict,
    new_fundamentals: dict,
    fixture: dict,
    *,
    legacy_provenance: Optional[dict] = None,
    new_provenance: Optional[dict] = None,
) -> dict:
    """Run both independent equivalence levels with no external acquisition."""
    if fixture.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("fixture contract_version does not match the dual-run contract")
    fundamental = compare_fundamental_contract(
        legacy_fundamentals,
        new_fundamentals,
        legacy_provenance=legacy_provenance,
        new_provenance=new_provenance,
    )
    fundamental["provenance"]["contractual_reference"] = copy.deepcopy(
        fixture.get("provenance", {}).get("input_lineage")
    )
    score = compare_score_equivalence(legacy_fundamentals, new_fundamentals, fixture)
    return {
        "contract_version": fixture["contract_version"],
        "fixture_version": fixture.get("fixture_version"),
        "tolerance_contract_version": TOLERANCE_CONTRACT_VERSION,
        "fundamental_contract": fundamental,
        "score": score,
        "semantic_gaps": copy.deepcopy(fixture.get("semantic_gaps", {})),
        "end_to_end_equivalent": False,
        "end_to_end_status": "NOT_ESTABLISHED",
    }
