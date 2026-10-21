import io
import logging
import math
import os
import re
import smtplib
import ssl
from collections import Counter, OrderedDict
from datetime import datetime
from email.message import EmailMessage
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


MISSING_VALUES = {"", "null", "none", "n/a", "na", "n.a.", "not available", "-"}
SENSITIVE_COLUMN = re.compile(r"(^|_)(name|email|phone|mobile|telephone|contact|address|emp_id|employee_id)(_|$)", re.I)
NUMERIC_COLUMN = re.compile(
    r"salary|rating|amount|total|count|average|avg|mean|median|minimum|maximum|sum|revenue|cost|percent|rate|number|quantity|headcount|employees",
    re.I,
)
ID_COLUMN = re.compile(r"(^|_)(id|emp_id|employee_id)(_|$)", re.I)
DATE_COLUMN = re.compile(r"date|time|timestamp", re.I)
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CHART_PALETTE = ["#176344", "#4d9472", "#80ad91", "#b0c9b7", "#d5e1d8", "#8a9b90"]
_REPORT_FONTS = None


def _report_fonts():
    global _REPORT_FONTS
    if _REPORT_FONTS is not None:
        return _REPORT_FONTS
    font_directory = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    font_files = {
        "regular": os.path.join(font_directory, "times.ttf"),
        "bold": os.path.join(font_directory, "timesbd.ttf"),
        "italic": os.path.join(font_directory, "timesi.ttf"),
        "bold_italic": os.path.join(font_directory, "timesbi.ttf"),
    }
    if all(os.path.isfile(path) for path in font_files.values()):
        names = {
            "regular": "TimesNewRoman",
            "bold": "TimesNewRoman-Bold",
            "italic": "TimesNewRoman-Italic",
            "bold_italic": "TimesNewRoman-BoldItalic",
        }
        for style, path in font_files.items():
            if names[style] not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(names[style], path))
        pdfmetrics.registerFontFamily(
            "TimesNewRoman", normal=names["regular"], bold=names["bold"],
            italic=names["italic"], boldItalic=names["bold_italic"],
        )
        _REPORT_FONTS = names
    else:
        _REPORT_FONTS = {
            "regular": "Times-Roman",
            "bold": "Times-Bold",
            "italic": "Times-Italic",
            "bold_italic": "Times-BoldItalic",
        }
    return _REPORT_FONTS


def is_sensitive_column(column):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
    return bool(SENSITIVE_COLUMN.search(normalized))


def safe_analysis_value(column, value):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
    if re.search(r"(^|_)status(_|$)", normalized) and isinstance(value, str):
        code = value.strip().upper()
        if code == "A":
            return "Active"
        if code in {"I", "T"}:
            return "Other status category"
    return value


def _missing(value):
    if value is None:
        return True
    return isinstance(value, str) and value.strip().casefold() in MISSING_VALUES


def _numeric_value(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\s,$€£₹]", "", value).rstrip("%")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


DATE_FORMAT_PAIRS = (
    ("%m/%d/%Y", "%m/%d/%Y"),
    ("%d/%m/%Y", "%d/%m/%Y"),
    ("%d-%m-%Y", "%d-%m-%Y"),
    ("%m-%d-%Y", "%m-%d-%Y"),
    ("%d-%b-%Y", "%d-%b-%Y"),
    ("%Y/%m/%d", "%Y/%m/%d"),
    ("%B %d, %Y", "%M %d, %Y"),
    ("%b %d, %Y", "%b %d, %Y"),
    ("%d %b %Y", "%d %b %Y"),
    ("%B %d %Y", "%M %d %Y"),
    ("%b %d %Y", "%b %d %Y"),
)


def normalize_date_values(value):
    if isinstance(value, datetime):
        return [value.date().isoformat()]
    if not isinstance(value, str):
        return []
    candidate = value.strip()
    values = []
    try:
        values.append(datetime.fromisoformat(candidate.replace("Z", "+00:00")).date().isoformat())
    except ValueError:
        pass
    for date_format, _ in DATE_FORMAT_PAIRS:
        try:
            values.append(datetime.strptime(candidate, date_format).date().isoformat())
        except ValueError:
            continue
    return list(dict.fromkeys(values))


def normalize_date_value(value):
    values = normalize_date_values(value)
    return values[0] if len(values) == 1 else None


def clean_query_result(result):
    """Return a normalized copy and quality counts without mutating query results."""
    columns = list(result.get("columns", []))
    source_rows = [list(row) for row in result.get("rows", [])]
    quality = {
        "missing_values": 0,
        "parse_failures_by_column": {},
        "ambiguous_values_by_column": {},
        "duplicate_rows": 0,
        "normalized_values": 0,
        "converted_types": 0,
        "source_rows": len(source_rows),
        "cleaned_rows": 0,
        "truncated": bool(result.get("truncated", False)),
        "source_unchanged": True,
    }
    source_counts = Counter(tuple(row) for row in source_rows)
    quality["duplicate_rows"] = sum(count - 1 for count in source_counts.values())
    if not columns or not source_rows:
        cleaned = dict(result)
        cleaned["rows"] = source_rows
        quality["cleaned_rows"] = len(source_rows)
        return cleaned, quality

    rows = [row[:len(columns)] + [None] * max(0, len(columns) - len(row)) for row in source_rows]
    for column_index, column in enumerate(columns):
        values = [row[column_index] for row in rows]
        cleaned_values = []
        for value in values:
            if _missing(value):
                cleaned_values.append(None)
                quality["missing_values"] += 1
            elif isinstance(value, str):
                normalized = " ".join(value.split())
                if normalized != value:
                    quality["normalized_values"] += 1
                cleaned_values.append(normalized)
            else:
                cleaned_values.append(value)

        normalized_column = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
        if DATE_COLUMN.search(normalized_column):
            candidates = [normalize_date_values(value) if value is not None else [] for value in cleaned_values]
            parsed = [values[0] if len(values) == 1 else None for values in candidates]
            for row_index, (old, options) in enumerate(zip(cleaned_values, candidates)):
                new = parsed[row_index]
                if old is not None and not options:
                    quality["converted_types"] += 1
                    quality["parse_failures_by_column"][str(column)] = (
                        quality["parse_failures_by_column"].get(str(column), 0) + 1
                    )
                    parsed[row_index] = old
                elif old is not None and len(options) > 1:
                    quality["ambiguous_values_by_column"][str(column)] = (
                        quality["ambiguous_values_by_column"].get(str(column), 0) + 1
                    )
                    parsed[row_index] = old
                elif old is not None and old != new:
                    quality["converted_types"] += 1
                cleaned_values[row_index] = parsed[row_index]
                rows[row_index][column_index] = parsed[row_index]
            continue

        nonempty = [value for value in cleaned_values if value is not None]
        numeric_values = [_numeric_value(value) for value in nonempty]
        numeric_successes = sum(value is not None for value in numeric_values)
        should_be_numeric = bool(NUMERIC_COLUMN.search(normalized_column)) and not ID_COLUMN.search(normalized_column)
        should_be_numeric = should_be_numeric or (
            not ID_COLUMN.search(normalized_column) and bool(nonempty) and numeric_successes / len(nonempty) >= 0.85
        )
        if should_be_numeric:
            parsed_values = [_numeric_value(value) if value is not None else None for value in cleaned_values]
            for row_index, (old, number) in enumerate(zip(cleaned_values, parsed_values)):
                if old is not None and number is None:
                    quality["converted_types"] += 1
                    quality["parse_failures_by_column"][str(column)] = (
                        quality["parse_failures_by_column"].get(str(column), 0) + 1
                    )
                    parsed_values[row_index] = old
                elif old is not None and not isinstance(old, (int, float)):
                    quality["converted_types"] += 1
                rows[row_index][column_index] = (
                    int(number) if number is not None and number.is_integer()
                    else number if number is not None else parsed_values[row_index]
                )
            continue

        if normalized_column.endswith("email"):
            for row_index, value in enumerate(cleaned_values):
                if isinstance(value, str):
                    normalized = value.lower()
                    if normalized != value:
                        quality["normalized_values"] += 1
                    cleaned_values[row_index] = normalized
        else:
            spellings = [value for value in cleaned_values if isinstance(value, str)]
            counts = Counter(value.casefold() for value in spellings)
            canonical = {}

            def spelling_rank(value):
                if value.isupper() and len(value) <= 5:
                    return 0
                if value.istitle():
                    return 1
                if value.islower():
                    return 2
                return 3

            for value in spellings:
                key = value.casefold()
                if (
                    key not in canonical
                    or counts[key] > counts[canonical[key].casefold()]
                    or counts[key] == counts[canonical[key].casefold()]
                    and spelling_rank(value) < spelling_rank(canonical[key])
                ):
                    canonical[key] = value
            for row_index, value in enumerate(cleaned_values):
                if isinstance(value, str):
                    replacement = canonical[value.casefold()]
                    if replacement != value:
                        quality["normalized_values"] += 1
                    cleaned_values[row_index] = replacement
        for row_index, value in enumerate(cleaned_values):
            rows[row_index][column_index] = value

    quality["cleaned_rows"] = len(rows)
    cleaned = dict(result)
    cleaned["columns"] = columns
    cleaned["rows"] = rows
    cleaned["row_count"] = len(rows)
    return cleaned, quality


_DEPARTMENT_ALIASES = {
    "engg": "Engineering",
    "engineering": "Engineering",
    "fin": "Finance",
    "finance": "Finance",
    "hr": "HR",
    "humanresources": "HR",
    "marketing": "Marketing",
    "mktg": "Marketing",
    "ops": "Operations",
    "operations": "Operations",
    "product": "Product",
    "productmanagement": "Product Management",
    "sales": "Sales",
    "salesdept": "Sales",
    "support": "Support",
}


def original_department_labels_requested(question):
    return bool(re.search(
        r"\b(?:original\s+(?:department\s+)?(?:labels?|values?)|raw\s+(?:department\s+)?(?:labels?|values?)|without\s+merging|"
        r"keep\b.{0,50}\bseparate|separate\b.{0,50}\bwithout\s+merging)\b",
        str(question or ""),
        re.IGNORECASE,
    ))


def normalize_department(value):
    if _missing(value):
        return "Missing department", True, False
    raw = " ".join(str(value).split())
    if raw.casefold().startswith("needs review:"):
        return raw, False, True
    if raw.casefold() == "missing department":
        return raw, False, False
    key = re.sub(r"[^a-z0-9]+", "", raw.casefold())
    canonical = _DEPARTMENT_ALIASES.get(key)
    if canonical:
        return canonical, raw != canonical, False
    return f"Needs review: {raw}", True, True


def configured_rating_scale():
    minimum = os.getenv("EMPLOYEE_RATING_MIN")
    maximum = os.getenv("EMPLOYEE_RATING_MAX")
    if not minimum or not maximum:
        return None
    try:
        lower, upper = float(minimum), float(maximum)
    except ValueError:
        return None
    return (lower, upper) if math.isfinite(lower) and math.isfinite(upper) and lower < upper else None


def _requested_top_n(question):
    match = re.search(r"\btop\s+(\d+)\b", str(question or ""), re.IGNORECASE)
    return max(1, int(match.group(1))) if match else None


def _rank_department_metrics(question, metrics, metric_key):
    ranked = sorted(
        (item for item in metrics if item.get(metric_key) is not None),
        key=lambda item: item[metric_key],
        reverse=True,
    )
    limit = _requested_top_n(question)
    return ranked[:limit] if limit is not None else ranked


def _record_reconciliation_table(quality):
    return {
        "title": "Source-to-analysis record reconciliation",
        "columns": ["Population", "Rows", "Definition"],
        "rows": [
            ["Source rows", quality["source_rows"], "All rows returned by the read-only source query."],
            ["Exact duplicate extra rows", quality["exact_duplicate_rows"], "Rows identical across every selected field beyond the first copy; removed in memory."],
            ["Rows after exact deduplication", quality["source_rows"] - quality["exact_duplicate_rows"], "Source rows less exact duplicate copies."],
            ["Rows under conflicting IDs", quality["rows_with_reused_employee_ids"], "All distinct rows whose nonmissing ID maps to multiple different records; all excluded, with no latest-row rule."],
            ["Rows with missing IDs", quality["records_with_missing_employee_id"], "Distinct rows without an ID; excluded from ID-based employee metrics."],
            ["Records included in metrics", quality["employee_rows_used_for_metrics"], "One distinct source row per nonmissing ID; IDs do not verify unique people."],
            ["Valid salary records", quality["valid_salary_observations"], "Included records with finite salary greater than zero."],
            ["Eligible records excluded from salary metrics", quality["employee_rows_used_for_metrics"] - quality["valid_salary_observations"], "Included records whose salary was missing, invalid, zero, or negative."],
            ["Source negative salary values", quality["negative_salary_values"], "Counted across source rows, including excluded and duplicate rows."],
            ["Source zero salary values", quality["zero_salary_values"], "Counted across source rows, including excluded and duplicate rows."],
            ["Source invalid salary values", quality["invalid_salary_values"], "Counted across source rows, including excluded and duplicate rows."],
        ],
    }


def build_employee_dataset(result):
    """Build shared employee metrics from raw rows without modifying the source."""
    columns = list(result.get("columns", []))
    source_rows = [list(row) for row in result.get("rows", [])]
    rows = [row[:len(columns)] + [None] * max(0, len(columns) - len(row)) for row in source_rows]
    indexes = {str(column).strip().casefold(): index for index, column in enumerate(columns)}
    rating_scale = configured_rating_scale()

    def value(row, column):
        index = indexes.get(column)
        return row[index] if index is not None and index < len(row) else None

    missing_by_column = {
        str(column): sum(_missing(row[index]) for row in rows)
        for index, column in enumerate(columns)
    }
    raw_counts = Counter(
        "<missing>" if _missing(value(row, "department")) else str(value(row, "department"))
        for row in rows
    )
    normalized_counts = Counter()
    department_review_counts = Counter()
    department_standardization_records = 0
    source_department_labels = set()
    for row in rows:
        raw_department = value(row, "department")
        normalized, changed, needs_review = normalize_department(raw_department)
        source_department_labels.add(normalized)
        normalized_counts[normalized] += 1
        department_standardization_records += int(changed)
        if needs_review:
            review_label = "<missing>" if _missing(raw_department) else str(raw_department)
            department_review_counts[review_label] += 1

    exact_counts = Counter(tuple(row) for row in rows)
    exact_duplicate_rows = sum(count - 1 for count in exact_counts.values())
    unique_rows = []
    seen_rows = set()
    for row in rows:
        signature = tuple(row)
        if signature not in seen_rows:
            seen_rows.add(signature)
            unique_rows.append(row)

    rows_by_id = OrderedDict()
    missing_id_rows = 0
    for row in unique_rows:
        employee_id = value(row, "emp_id")
        if _missing(employee_id):
            missing_id_rows += 1
            continue
        rows_by_id.setdefault(employee_id, []).append(row)
    duplicate_id_groups = [group for group in rows_by_id.values() if len(group) > 1]
    eligible_rows = [group[0] for group in rows_by_id.values() if len(group) == 1]

    salary_values = []
    rating_values = []
    numeric_rating_values = []
    paired_rows = []
    valid_dates = []
    date_source_counts = Counter()
    date_classifications = []
    department_groups = OrderedDict()
    raw_department_groups = OrderedDict()
    employee_salary_comparison_records = []
    for row in rows:
        raw_department = value(row, "department")
        raw_label = "Missing department" if _missing(raw_department) else str(raw_department)
        raw_department_groups.setdefault(raw_label, {
            "department": raw_label,
            "record_count": 0,
            "employee_count": 0,
            "salary_values": [],
            "rating_values": [],
        })["record_count"] += 1
    negative_salary_count = 0
    zero_salary_count = 0
    invalid_salary_count = 0
    invalid_rating_count = 0
    out_of_range_rating_count = 0
    unparseable_date_count = 0
    ambiguous_date_count = 0
    employee_missing_salary = 0
    employee_missing_rating = 0
    employee_missing_date = 0
    employee_unparseable_date = 0
    employee_ambiguous_date = 0
    employee_negative_salary = 0
    employee_zero_salary = 0
    employee_out_of_range_rating = 0
    for source_row_number, row in enumerate(rows, start=1):
        salary_value = value(row, "salary")
        rating_value = value(row, "rating")
        parsed_salary = _numeric_value(salary_value)
        parsed_rating = _numeric_value(rating_value)
        if not _missing(salary_value):
            if parsed_salary is None:
                invalid_salary_count += 1
            elif parsed_salary < 0:
                negative_salary_count += 1
            elif parsed_salary == 0:
                zero_salary_count += 1
        if not _missing(rating_value):
            if parsed_rating is None:
                invalid_rating_count += 1
            else:
                numeric_rating_values.append(parsed_rating)
            if rating_scale and parsed_rating is not None and not rating_scale[0] <= parsed_rating <= rating_scale[1]:
                out_of_range_rating_count += 1
        date_value = value(row, "join_date")
        if _missing(date_value):
            category = "missing"
            if date_value is None:
                reason = "Database NULL value."
            elif not str(date_value).strip():
                reason = "Blank or whitespace-only value."
            else:
                reason = f"Recognized missing-value marker: {str(date_value).strip()}."
            date_candidates = []
        else:
            date_candidates = normalize_date_values(date_value)
            if len(date_candidates) == 1:
                category = "valid"
                reason = f"One supported interpretation: {date_candidates[0]}."
            elif len(date_candidates) > 1:
                category = "ambiguous"
                reason = "Multiple locale-valid interpretations: " + ", ".join(date_candidates) + "."
            else:
                category = "invalid"
                reason = "No supported date format produced a valid calendar date."
        date_source_counts[category] += 1
        date_classifications.append({
            "source_row": source_row_number,
            "original_value": date_value,
            "category": category,
            "reason": reason,
        })
        if category == "invalid":
            unparseable_date_count += 1
        elif category == "ambiguous":
            ambiguous_date_count += 1

    for row in eligible_rows:
        salary_value = _numeric_value(value(row, "salary"))
        rating_value = _numeric_value(value(row, "rating"))
        source_date = value(row, "join_date")
        date_candidates = normalize_date_values(source_date) if not _missing(source_date) else []
        date_value = date_candidates[0] if len(date_candidates) == 1 else None
        employee_missing_salary += int(_missing(value(row, "salary")))
        employee_missing_rating += int(_missing(value(row, "rating")))
        employee_missing_date += int(_missing(value(row, "join_date")))
        employee_negative_salary += int(salary_value is not None and salary_value < 0)
        employee_zero_salary += int(salary_value == 0)
        employee_out_of_range_rating += int(
            bool(rating_scale) and rating_value is not None
            and not rating_scale[0] <= rating_value <= rating_scale[1]
        )
        employee_unparseable_date += int(not _missing(source_date) and not date_candidates)
        employee_ambiguous_date += int(len(date_candidates) > 1)
        if salary_value is not None and salary_value > 0:
            salary_values.append(salary_value)
        if rating_scale and rating_value is not None and rating_scale[0] <= rating_value <= rating_scale[1]:
            rating_values.append(rating_value)
        if salary_value is not None and salary_value > 0 and rating_scale and rating_value is not None and rating_scale[0] <= rating_value <= rating_scale[1]:
            paired_rows.append((salary_value, rating_value))
        if date_value:
            valid_dates.append(date_value)

        department, _, _ = normalize_department(value(row, "department"))
        if salary_value is not None and salary_value > 0:
            employee_name = value(row, "name")
            employee_salary_comparison_records.append({
                "employee_id": value(row, "emp_id"),
                "name": None if _missing(employee_name) else " ".join(str(employee_name).split()),
                "department": department,
                "salary": salary_value,
            })
        raw_department = value(row, "department")
        raw_label = "Missing department" if _missing(raw_department) else str(raw_department)
        raw_group = raw_department_groups[raw_label]
        raw_group["employee_count"] += 1
        group = department_groups.setdefault(department, {
            "department": department,
            "employee_count": 0,
            "salary_values": [],
            "rating_values": [],
        })
        group["employee_count"] += 1
        if salary_value is not None and salary_value > 0:
            group["salary_values"].append(salary_value)
        if rating_scale and rating_value is not None and rating_scale[0] <= rating_value <= rating_scale[1]:
            group["rating_values"].append(rating_value)
            raw_group["rating_values"].append(rating_value)
        if salary_value is not None and salary_value > 0:
            raw_group["salary_values"].append(salary_value)

    department_metrics = []
    department_salary_values = {}
    for group in department_groups.values():
        salaries = group.pop("salary_values")
        ratings = group.pop("rating_values")
        department_salary_values[group["department"]] = list(salaries)
        group["valid_salary_observations"] = len(salaries)
        group["average_salary"] = sum(salaries) / len(salaries) if salaries else None
        group["valid_rating_observations"] = len(ratings) if rating_scale else None
        group["average_rating"] = sum(ratings) / len(ratings) if ratings else None
        department_metrics.append(group)

    raw_department_metrics = []
    for group in raw_department_groups.values():
        salaries = group.pop("salary_values")
        ratings = group.pop("rating_values")
        group["valid_salary_observations"] = len(salaries)
        group["average_salary"] = sum(salaries) / len(salaries) if salaries else None
        group["valid_rating_observations"] = len(ratings) if rating_scale else None
        group["average_rating"] = sum(ratings) / len(ratings) if ratings else None
        raw_department_metrics.append(group)

    quality = {
        "source_rows": len(rows),
        "source_valid_joining_dates": date_source_counts["valid"],
        "source_missing_joining_dates": date_source_counts["missing"],
        "source_invalid_joining_dates": date_source_counts["invalid"],
        "source_ambiguous_joining_dates": date_source_counts["ambiguous"],
        "exact_duplicate_rows": exact_duplicate_rows,
        "distinct_employee_ids": len(rows_by_id),
        "duplicate_employee_id_count": len(duplicate_id_groups),
        "duplicate_employee_extra_rows": sum(len(group) - 1 for group in duplicate_id_groups),
        "rows_with_reused_employee_ids": sum(len(group) for group in duplicate_id_groups),
        "records_excluded_for_reused_employee_ids": sum(len(group) for group in duplicate_id_groups),
        "records_with_missing_employee_id": missing_id_rows,
        "employee_rows_used_for_metrics": len(eligible_rows),
        "valid_salary_observations": len(salary_values),
        "valid_rating_observations": len(rating_values) if rating_scale else None,
        "valid_salary_rating_pairs": len(paired_rows) if rating_scale else None,
        "average_salary": sum(salary_values) / len(salary_values) if salary_values else None,
        "average_rating": sum(rating_values) / len(rating_values) if rating_values else None,
        "rating_scale": rating_scale,
        "numeric_rating_observations": len(numeric_rating_values),
        "numeric_rating_minimum": min(numeric_rating_values) if numeric_rating_values else None,
        "numeric_rating_maximum": max(numeric_rating_values) if numeric_rating_values else None,
        "negative_salary_values": negative_salary_count,
        "zero_salary_values": zero_salary_count,
        "invalid_salary_values": invalid_salary_count,
        "invalid_rating_values": invalid_rating_count,
        "out_of_range_rating_values": out_of_range_rating_count if rating_scale else None,
        "employee_missing_salary": employee_missing_salary,
        "employee_missing_rating": employee_missing_rating,
        "employee_missing_joining_date": employee_missing_date,
        "employee_ambiguous_joining_dates": employee_ambiguous_date,
        "employee_negative_salary_values": employee_negative_salary,
        "employee_zero_salary_values": employee_zero_salary,
        "employee_out_of_range_rating_values": employee_out_of_range_rating,
        "employee_unparseable_joining_dates": employee_unparseable_date,
        "valid_joining_dates": len(valid_dates),
        "unparseable_joining_dates": unparseable_date_count,
        "ambiguous_joining_dates": ambiguous_date_count,
        "missing_by_column": missing_by_column,
        "raw_department_counts": dict(raw_counts),
        "normalized_department_counts": dict(normalized_counts),
        "records_needing_department_standardization": department_standardization_records,
        "department_mapping_review_counts": dict(department_review_counts),
    }
    employee_salary_records = []
    for row in eligible_rows:
        salary = _numeric_value(value(row, "salary"))
        name = value(row, "name")
        if salary is not None and salary > 0 and not _missing(name):
            department, _, _ = normalize_department(value(row, "department"))
            employee_salary_records.append({
                "employee_id": value(row, "emp_id"),
                "name": str(name).strip(),
                "department": department,
                "salary": salary,
            })
    return {
        "quality": quality,
        "source_columns": columns,
        "source_rows": rows,
        "date_classifications": date_classifications,
        "salary_values": salary_values,
        "rating_values": rating_values,
        "salary_rating_pairs": paired_rows,
        "valid_joining_dates": valid_dates,
        "department_metrics": department_metrics,
        "raw_department_metrics": raw_department_metrics,
        "department_salary_values": department_salary_values,
        "source_department_labels": sorted(source_department_labels, key=str.casefold),
        "employee_salary_records": employee_salary_records,
        "employee_salary_comparison_records": employee_salary_comparison_records,
        "truncated": bool(result.get("truncated", False)),
    }


def build_employee_chart(dataset, chart_type, question=""):
    if chart_type == "histogram":
        values = dataset["salary_values"]
        return build_chart({
            "columns": ["salary"],
            "rows": [[value] for value in values],
        }, "histogram")

    if chart_type == "correlation_heatmap":
        pairs = dataset["salary_rating_pairs"]
        if len(pairs) < 2:
            return None
        salaries = [pair[0] for pair in pairs]
        ratings = [pair[1] for pair in pairs]
        salary_mean = sum(salaries) / len(salaries)
        rating_mean = sum(ratings) / len(ratings)
        covariance = sum((salary - salary_mean) * (rating - rating_mean) for salary, rating in pairs)
        salary_variance = sum((salary - salary_mean) ** 2 for salary in salaries)
        rating_variance = sum((rating - rating_mean) ** 2 for rating in ratings)
        denominator = math.sqrt(salary_variance * rating_variance)
        if denominator == 0:
            return None
        correlation = max(-1.0, min(1.0, covariance / denominator))
        return {
            "type": "heatmap",
            "title": "Correlation between salary and rating",
            "metric": "Pearson correlation coefficient",
            "dimension": "Salary and rating",
            "unit": "r",
            "labels": [],
            "values": [],
            "x_labels": ["Salary", "Rating"],
            "y_labels": ["Salary", "Rating"],
            "matrix": [[1.0, round(correlation, 4)], [round(correlation, 4), 1.0]],
            "x_label": "Measure",
            "y_label": "Measure",
            "observations": len(pairs),
        }

    if chart_type == "salary_box":
        result = {
            "columns": ["department", "salary"],
            "rows": [
                [department, salary]
                for department, salaries in dataset["department_salary_values"].items()
                for salary in salaries
            ],
        }
        chart = build_chart(result, "box")
        if chart:
            chart["excluded_departments"] = [
                department for department in dataset["source_department_labels"]
                if not dataset["department_salary_values"].get(department)
            ]
        return chart

    if chart_type == "scatter":
        pairs = dataset["salary_rating_pairs"]
        if len(pairs) < 3:
            return None
        return build_chart({
            "columns": ["salary", "rating"],
            "rows": [[salary, rating] for salary, rating in pairs],
        }, "scatter")

    if chart_type == "department_metrics":
        lowered = str(question).casefold()
        metric = "average_salary" if "salary" in lowered else "average_rating" if "rating" in lowered else "employee_count"
        raw_labels = original_department_labels_requested(question)
        metrics = dataset["raw_department_metrics"] if raw_labels else dataset["department_metrics"]
        ranked = _rank_department_metrics(question, metrics, metric)
        rows = [[item["department"], item[metric]] for item in ranked]
        requested_type = requested_chart_type(question)
        if requested_type == "pie" and metric == "average_salary":
            requested_type = "bar"
        return build_chart({
            "columns": ["department_label" if raw_labels else "department", metric],
            "rows": rows,
            "question": question,
        }, requested_type)

    if chart_type == "employee_salary_ranking":
        ranked = sorted(dataset["employee_salary_records"], key=lambda item: item["salary"], reverse=True)
        limit = _requested_top_n(question) or 5
        rows = [[f"{item['name']} ({item['department']})", item["salary"]] for item in ranked[:limit]]
        if not rows:
            return None
        return build_chart({
            "columns": ["employee", "salary"],
            "rows": rows,
            "question": question,
        }, requested_chart_type(question) or "bar")

    if chart_type == "monthly_hiring":
        monthly_counts = Counter(value[:7] for value in dataset["valid_joining_dates"])
        requested_years = [int(year) for year in re.findall(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", str(question or ""))]
        if not monthly_counts and not requested_years:
            return None
        if requested_years:
            first_month = min(requested_years) * 12
            last_month_index = (max(requested_years) + 1) * 12 - 1
            months = []
            for month_index in range(first_month, last_month_index + 1):
                year, month_number = divmod(month_index, 12)
                months.append(f"{year:04d}-{month_number + 1:02d}")
            period = f"{min(requested_years)}" if len(set(requested_years)) == 1 else f"{min(requested_years)}–{max(requested_years)}"
        else:
            last_year, last_month = map(int, max(monthly_counts).split("-"))
            months = []
            for offset in range(11, -1, -1):
                month_index = last_year * 12 + last_month - 1 - offset
                year, month_number = divmod(month_index, 12)
                months.append(f"{year:04d}-{month_number + 1:02d}")
            period = None
        result = {
            "columns": ["join_date", "employee_count"],
            "rows": [[month, monthly_counts.get(month, 0)] for month in months],
            "question": question or "monthly hiring trend",
        }
        chart = build_chart(result, "line")
        if chart:
            chart["title"] = f"Monthly hires ({period})" if period else "Monthly hires (12 most recent months)"
            chart["x_label"] = "Month"
            chart["y_label"] = "Hires"
            chart["unit"] = "employees"
            chart["observations"] = sum(monthly_counts.get(month, 0) for month in months)
            if period:
                chart["period"] = period
        return chart

    return None


def build_employee_analysis(question, intent, dataset):
    quality = dataset["quality"]
    employee_count = quality["employee_rows_used_for_metrics"]
    duplicate_rows = quality["rows_with_reused_employee_ids"]
    limitations = []
    tables = []
    chart = None
    chart_notice = None
    chart_failure_type = None
    salary_average = quality["average_salary"]
    rating_average = quality["average_rating"]
    rating_count = quality["valid_rating_observations"]
    pair_count = quality["valid_salary_rating_pairs"]
    rating_count_label = f"{rating_count:,}" if rating_count is not None else "Not assessed"
    pair_count_label = f"{pair_count:,}" if pair_count is not None else "Not assessed"
    rating_scale_label = f"{quality['rating_scale'][0]:g}–{quality['rating_scale'][1]:g}" if quality["rating_scale"] else "Not configured"

    def make_chart(chart_type):
        nonlocal chart_failure_type
        try:
            return build_employee_chart(dataset, chart_type, question)
        except Exception as error:
            chart_failure_type = type(error).__name__
            return None

    if quality["duplicate_employee_id_count"] and intent not in {"missing_values", "date_diagnostics"}:
        limitations.append(
            f"{duplicate_rows} distinct source records share a reused employee ID; all were excluded from employee-level metrics."
        )
    if dataset["truncated"]:
        limitations.append("The employee source result was truncated, so counts and charts may not cover the complete source.")

    if intent == "employee_salary_ranking":
        ranked = sorted(dataset["employee_salary_records"], key=lambda item: item["salary"], reverse=True)
        limit = _requested_top_n(question) or 5
        ranked = ranked[:limit]
        if ranked:
            answer = f"Top {len(ranked)} employees by validated salary: " + "; ".join(
                f"{item['name']} ({item['department']}): {item['salary']:,.2f}"
                for item in ranked
            ) + "."
        else:
            answer = "No eligible employee salary records are available for a ranking."
        insights = [
            f"The ranking uses positive finite salaries for distinct, eligible employee IDs; {quality['rows_with_reused_employee_ids']:,} rows under reused IDs were excluded."
        ]
        if quality["negative_salary_values"] or quality["zero_salary_values"]:
            limitations.append(
                f"{quality['negative_salary_values']:,} negative and {quality['zero_salary_values']:,} zero source salaries were excluded from the ranking."
            )
        tables.append({
            "title": f"Top {len(ranked)} employees by salary",
            "columns": ["Name", "Department", "Salary"],
            "rows": [[item["name"], item["department"], item["salary"]] for item in ranked],
        })

    elif intent == "date_diagnostics":
        category_labels = {
            "valid": "Valid",
            "missing": "Missing",
            "invalid": "Invalid",
            "ambiguous": "Ambiguous",
        }
        categories = ["valid", "missing", "invalid", "ambiguous"]
        counts = {category: quality[f"source_{category}_joining_dates"] for category in categories}
        tables.append({
            "title": "Joining-date classification across all source rows",
            "columns": ["Category", "Source rows"],
            "rows": [[category_labels[category], counts[category]] for category in categories],
        })
        requested_category = next((
            category for category in ("ambiguous", "invalid", "missing", "valid")
            if re.search(rf"\b{category}\b", question, re.IGNORECASE)
        ), None)
        if requested_category:
            selected_examples = [
                item for item in dataset["date_classifications"]
                if item["category"] == requested_category
            ][:10]
        else:
            selected_examples = []
            for category in categories:
                selected_examples.extend([
                    item for item in dataset["date_classifications"]
                    if item["category"] == category
                ][:3])
        tables.append({
            "title": f"{category_labels[requested_category]} joining-date examples" if requested_category else "Joining-date examples by category",
            "columns": ["Source row", "Original value", "Classification reason"],
            "rows": [[item["source_row"], item["original_value"], item["reason"]] for item in selected_examples],
        })
        if sum(counts.values()) != quality["source_rows"]:
            limitations.append("Date categories did not reconcile to the source-row total; review the date classifier before relying on the counts.")
        blank_missing = sum(
            item["category"] == "missing" and isinstance(item["original_value"], str)
            and not item["original_value"].strip()
            for item in dataset["date_classifications"]
        )
        marker_missing = counts["missing"] - blank_missing - sum(
            item["category"] == "missing" and item["original_value"] is None
            for item in dataset["date_classifications"]
        )
        eligible_missing = quality["employee_missing_joining_date"]
        excluded_missing = counts["missing"] - eligible_missing
        answer = (
            f"Across {quality['source_rows']:,} source rows, joining dates classify as "
            f"{counts['valid']:,} valid, {counts['missing']:,} missing, {counts['invalid']:,} invalid, "
            f"and {counts['ambiguous']:,} ambiguous; the categories total {sum(counts.values()):,}. "
            f"Missing includes {blank_missing:,} blank values and {marker_missing:,} recognized markers. "
            f"Employee-level metrics use {employee_count:,} eligible rows and have {eligible_missing:,} missing dates; "
            f"{excluded_missing:,} source rows with missing dates are outside that employee-level population."
        )
        insights = [
            "Invalid means no supported format yields a valid calendar date; ambiguous means multiple supported locale interpretations remain possible.",
        ]

    elif intent == "missing_values":
        source_columns = dataset["source_columns"]
        source_rows = dataset["source_rows"]
        column_indexes = {str(column).strip().casefold(): index for index, column in enumerate(source_columns)}
        requested_fields = [
            field for field, pattern in (
                ("email", r"\bemail\b"),
                ("phone", r"\b(?:phone|mobile|telephone)\b"),
                ("name", r"\bname\b"),
                ("department", r"\b(?:department|dept)\b"),
                ("salary", r"\b(?:salary|pay)\b"),
                ("join_date", r"\b(?:join|joining|hire|hiring)[_ ]?date\b"),
                ("city", r"\bcity\b"),
                ("rating", r"\brating\b"),
                ("status", r"\bstatus\b"),
            )
            if re.search(pattern, question, re.IGNORECASE) and field in column_indexes
        ]
        if not requested_fields:
            requested_fields = [field for field in column_indexes if field != "emp_id"]
        missing_rows = [
            row for row in source_rows
            if (
                any(_missing(row[column_indexes[field]]) for field in requested_fields)
                if re.search(r"\bor\b", question, re.IGNORECASE)
                else all(_missing(row[column_indexes[field]]) for field in requested_fields)
            )
        ]
        count_request = bool(re.search(r"\b(?:count|how many|number of)\b", question, re.IGNORECASE))
        if count_request:
            counts = [[field, quality["missing_by_column"].get(source_columns[column_indexes[field]], 0)]
                      for field in requested_fields]
            answer = "Missing-value counts are calculated across source rows without deduplication: " + "; ".join(
                f"{field}: {count:,}" for field, count in counts
            ) + "."
            tables.append({
                "title": "Missing values by requested field",
                "columns": ["Field", "Missing source rows"],
                "rows": counts,
            })
        else:
            conjunction = "or" if re.search(r"\bor\b", question, re.IGNORECASE) else "and"
            answer = (
                f"Found {len(missing_rows):,} source rows with missing "
                f"{f' {conjunction} '.join(requested_fields)}; matching is applied to the original rows without deduplication."
            )
            tables.append({
                "title": "Source records with requested missing fields",
                "columns": source_columns,
                "rows": missing_rows,
            })
        insights = [
            "Missing includes null, blank, whitespace-only, and recognized missing-value markers; source rows are not deduplicated."
        ]

    elif intent == "department_employee_salary_ranking":
        top_n = _requested_top_n(question) or 3
        dense_requested = bool(re.search(r"\bdense[\s_]*rank\b", question, re.IGNORECASE))
        rank_method = "dense" if dense_requested or not re.search(r"\brank\s*\(|\brank\b", question, re.IGNORECASE) else "rank"
        department_groups = {}
        for item in dataset["employee_salary_records"]:
            department_groups.setdefault(item["department"], []).append(item)
        ranked_rows = []
        for department in sorted(department_groups, key=str.casefold):
            employees = sorted(
                department_groups[department],
                key=lambda item: (-item["salary"], str(item["employee_id"])),
            )
            salary_levels = sorted({item["salary"] for item in employees}, reverse=True)
            if rank_method == "rank":
                salary_ranks = {}
                position = 1
                for salary in salary_levels:
                    salary_ranks[salary] = position
                    position += sum(item["salary"] == salary for item in employees)
            else:
                salary_ranks = {
                    salary: rank for rank, salary in enumerate(salary_levels, start=1)
                }
            for item in employees:
                rank = salary_ranks[item["salary"]]
                if rank > top_n:
                    break
                ranked_rows.append([
                    item["employee_id"], item["name"], department, item["salary"], rank,
                ])
        answer = (
            f"Found {len(ranked_rows):,} employees within the top {top_n} {rank_method} salary ranks "
            "in each normalized department. Salary ties are included; no result limit applied."
        )
        if dataset["truncated"]:
            answer += " The source result was truncated, so additional matches may exist."
        tables.append({
            "title": f"Top {top_n} salary ranks in each department",
            "columns": ["Employee ID", "Name", "Department", "Salary", "Rank" if rank_method == "rank" else "Dense rank"],
            "rows": ranked_rows,
        })
        insights = [
            "Ranks use positive finite salaries for distinct eligible employee IDs; ties share a rank and all tied employees are retained."
        ]

    elif intent == "employees_above_department_salary_average":
        averages = {
            item["department"]: item["average_salary"]
            for item in dataset["department_metrics"]
            if item["average_salary"] is not None
        }
        comparisons = [
            {
                **item,
                "department_average": averages[item["department"]],
            }
            for item in dataset["employee_salary_comparison_records"]
            if item["department"] in averages
            and item["salary"] > averages[item["department"]]
        ]
        comparisons.sort(key=lambda item: (-item["salary"], str(item["employee_id"])))
        answer = (
            f"Found {len(comparisons):,} eligible employees whose positive finite salary exceeds their normalized department's "
            "average valid salary. No result limit applied."
        )
        if dataset["truncated"]:
            answer += " The full source result was truncated, so additional matches may exist."
        tables.append({
            "title": "Employees earning above their department average",
            "columns": ["Employee ID", "Name", "Department", "Salary", "Department average salary"],
            "rows": [[
                item["employee_id"], item["name"], item["department"], item["salary"], item["department_average"],
            ] for item in comparisons],
        })
        insights = []
        limitations = [
            "Employee-level comparisons use records with one distinct row per nonmissing employee ID and positive finite salaries; department aliases use the existing normalization rules."
        ]

    elif intent == "count_reconciliation":
        answer = (
            f"The full source has {quality['source_rows']:,} rows but {quality['distinct_employee_ids']:,} distinct employee IDs; "
            f"one ID is reused across {duplicate_rows:,} differing records, leaving {employee_count:,} eligible records. "
            f"Validated counts are {quality['valid_salary_observations']:,} valid salaries; rating counts are not assessed without a configured scale, "
            f"so 492 is not reproduced by these full-source rules."
        )
        insights = [
            f"The source-row count is {quality['source_rows']:,}; distinct employee IDs number {quality['distinct_employee_ids']:,}.",
            f"Employee-level metrics exclude {duplicate_rows:,} distinct records under the reused ID and use {employee_count:,} eligible records; IDs do not verify unique people.",
            "The exact query or filter behind 492 is not available here, so its additional exclusions cannot be verified.",
        ]
        tables.append({
            "title": "Full-source count reconciliation",
            "columns": ["Population", "Count", "Definition"],
            "rows": [
                ["Source rows", quality["source_rows"], "All source records returned by the read-only full-table query."],
                ["Distinct employee IDs", quality["distinct_employee_ids"], "Unique nonmissing emp_id values."],
                ["Rows under reused IDs", duplicate_rows, "Differing source records for IDs occurring more than once; excluded from employee-level metrics."],
                ["Records used for metrics", employee_count, "One distinct source row per nonmissing ID; this does not verify unique people."],
                ["Valid salary observations", quality["valid_salary_observations"], "Positive finite salaries from the employee-level population."],
                ["Numeric rating observations", quality["numeric_rating_observations"], "Numeric source values; no scale is configured."],
                ["Configured rating scale", rating_scale_label, "Rating metrics require an explicitly documented scale."],
                ["Previously cited count", 492, "Not reproduced by these full-source rules; the prior filter is needed to explain it."],
            ],
        })
        limitations.append("The exact earlier SQL/filter that produced 492 is not available, so no specific exclusion can be assigned to that value.")

    elif intent == "data_quality":
        answer = (
            f"The source contains {quality['source_rows']:,} rows and {quality['distinct_employee_ids']:,} distinct employee IDs. "
            f"There are {quality['exact_duplicate_rows']:,} exact duplicate rows; one ID is reused across "
            f"{duplicate_rows:,} distinct records, which are excluded from employee-level metrics."
        )
        overview = [
            ["Source rows", quality["source_rows"], "Physical source records before duplicate handling."],
            ["Distinct employee IDs", quality["distinct_employee_ids"], "Distinct nonmissing emp_id values; a repeated ID counts once."],
            ["Exact duplicate rows", quality["exact_duplicate_rows"], "Extra full rows identical across every selected source field."],
            ["Repeated-ID groups", quality["duplicate_employee_id_count"], "Distinct employee IDs appearing in multiple different records."],
            ["Rows under repeated IDs", duplicate_rows, "All rows for reused IDs, excluded to avoid selecting an arbitrary employee record."],
            ["Extra rows under repeated IDs", quality["duplicate_employee_extra_rows"], "Rows beyond one per repeated employee ID."],
            ["Records used for metrics", employee_count, "One distinct source row per nonmissing ID after exact-row deduplication; IDs do not verify unique people."],
            ["Valid salary observations", quality["valid_salary_observations"], "Finite salaries greater than zero; missing, invalid, zero, and negative values are excluded."],
            ["Numeric rating observations", quality["numeric_rating_observations"], "Numeric source values; not classified without a documented scale."],
            ["Observed numeric rating range", f"{quality['numeric_rating_minimum']:g} to {quality['numeric_rating_maximum']:g}" if quality["numeric_rating_observations"] else "Unavailable", "Observed values do not establish the intended rating scale."],
            ["Configured rating scale", rating_scale_label, "Rating averages and out-of-range counts require a documented scale."],
            ["Validated rating observations", rating_count_label, "Requires an explicitly configured scale."],
            ["Valid joining dates", quality["valid_joining_dates"], "Employee records whose nonmissing joining date parses successfully."],
            ["Ambiguous joining dates", quality["ambiguous_joining_dates"], "Numeric dates matching both day-first and month-first formats; excluded from date-based metrics without a locale."],
            ["Negative salary values", quality["negative_salary_values"], "Raw source salary values below zero; never replaced with zero."],
            ["Zero salary values", quality["zero_salary_values"], "Raw source salary values equal to zero; excluded from valid salary metrics."],
            ["Out-of-range ratings", quality["out_of_range_rating_values"] if quality["rating_scale"] else "Not assessed", "Requires an explicitly configured scale."],
            ["Unparseable joining dates", quality["unparseable_joining_dates"], "Nonmissing source date values that could not be parsed."],
            ["Records needing department standardization", quality["records_needing_department_standardization"], "Rows changed to a canonical label or explicitly flagged for review."],
            ["Department records flagged for review", sum(quality["department_mapping_review_counts"].values()), "Rows with missing or uncertain department labels; not merged into a guessed department."],
        ]
        tables.append({
            "title": "Quality measures and definitions",
            "columns": ["Measure", "Count", "Definition"],
            "rows": overview,
        })
        tables.append({
            "title": "Missing values by column",
            "columns": ["Column", "Missing count", "Definition"],
            "rows": [[column, count, "Null, blank, or recognized missing-value marker in source rows."]
                     for column, count in quality["missing_by_column"].items()],
        })
        department_rows = []
        for raw_label, raw_count in sorted(quality["raw_department_counts"].items(), key=lambda item: item[0].casefold()):
            raw_value = None if raw_label == "<missing>" else raw_label
            normalized, _, needs_review = normalize_department(raw_value)
            department_rows.append([
                raw_label,
                raw_count,
                normalized,
                quality["normalized_department_counts"].get(normalized, 0),
                "Needs review" if needs_review else "Standardized",
            ])
        tables.append({
            "title": "Raw and normalized department counts",
            "columns": ["Raw label", "Raw rows", "Normalized label", "Normalized rows", "Mapping"],
            "rows": department_rows,
        })
        limitations.append(
            "Department normalization merges only listed, unambiguous names and formatting variants; labels marked Needs review remain separate."
        )
        insights = [
            f"{quality['exact_duplicate_rows']:,} exact duplicate rows were found; {quality['duplicate_employee_id_count']:,} employee-ID group(s) have differing records.",
            f"{quality['records_needing_department_standardization']:,} source records need department standardization; uncertain labels are flagged instead of merged.",
            f"There are {quality['valid_salary_observations']:,} valid salary observations; rating scale is {'configured' if quality['rating_scale'] else 'not configured'}.",
        ]
        recommendations = ["Review the flagged department labels and reused employee ID before treating those records as canonical employee history."]

    elif intent == "histogram":
        chart = make_chart("histogram")
        if not dataset["salary_values"]:
            answer = "No valid positive salary observations are available for a histogram."
            chart_notice = "A salary histogram requires at least one finite salary greater than zero."
            insights = []
        else:
            answer = (
                f"The salary histogram uses {quality['valid_salary_observations']:,} valid observations from "
                f"{employee_count:,} eligible records. The mean salary is {salary_average:,.2f} "
                "across those valid observations."
            )
            insights = [
                f"{quality['valid_salary_observations']:,} salaries are finite and greater than zero.",
                f"Mean salary is {salary_average:,.2f}, calculated directly from valid employee records.",
                f"{quality['employee_negative_salary_values'] + quality['employee_zero_salary_values']:,} salary values were excluded because they were negative or zero; missing values were not imputed.",
            ]
            if chart:
                tables.append({
                    "title": "Salary histogram bins",
                    "columns": ["Salary interval", "Employee observations"],
                    "rows": [[label, count] for label, count in zip(chart["labels"], chart["values"])],
                })

    elif intent == "salary_box":
        chart = make_chart("salary_box")
        if not dataset["salary_values"]:
            answer = "No valid positive salary observations are available for a box plot."
            chart_notice = "A salary box plot requires at least one finite salary greater than zero."
            insights = []
        else:
            answer = (
                f"The salary box plot uses {quality['valid_salary_observations']:,} valid observations "
                f"from {employee_count:,} eligible records; quartiles and 1.5-IQR whiskers are computed from raw salaries."
            )
            insights = [
                f"{quality['valid_salary_observations']:,} positive finite salaries are included.",
                "Whiskers stop at the most extreme values within 1.5 times the interquartile range; outside values are shown as outliers.",
            ]
            if chart:
                excluded_departments = chart.get("excluded_departments", [])
                if excluded_departments:
                    limitations.append(
                        "Departments without positive finite salaries are excluded from the box plot: "
                        + ", ".join(excluded_departments)
                        + "."
                    )
                tables.append({
                    "title": "Salary quartiles by normalized department",
                    "columns": ["Department", "Lower whisker", "Q1", "Median", "Q3", "Upper whisker", "Outlier count"],
                    "rows": [[
                        group["label"], group["min"], group["q1"], group["median"], group["q3"],
                        group["max"], len(group["outliers"]),
                    ] for group in chart["groups"]],
                })

    elif intent == "department_metrics":
        chart = make_chart("department_metrics")
        metric_key = "average_salary" if "salary" in question.casefold() else "average_rating" if "rating" in question.casefold() else "employee_count"
        raw_labels = original_department_labels_requested(question)
        department_metrics = dataset["raw_department_metrics"] if raw_labels else dataset["department_metrics"]
        ranked = _rank_department_metrics(question, department_metrics, metric_key)
        answer = direct_department_answer(question, dataset)
        insights = []
        if ranked and metric_key == "average_salary":
            top_item = ranked[0]
            insights.append(
                f"{top_item['department']} has the highest displayed department average at {top_item['average_salary']:,.2f}, "
                f"based on {top_item['valid_salary_observations']:,} valid salary records."
            )
            if len(ranked) > 1:
                low_item = ranked[-1]
                spread = top_item["average_salary"] - low_item["average_salary"]
                insights.append(
                    f"The displayed top-group spread is {spread:,.2f}, from {top_item['department']} "
                    f"to {low_item['department']}; these are descriptive averages, not a cause or pay-equity finding."
                )
        insights.append(
            f"The source contains {quality['source_rows']:,} rows; {employee_count:,} records with one distinct row per nonmissing ID were eligible for employee-level metrics."
        )
        metric_columns = ["Department label", "Eligible employee count"]
        if metric_key == "average_salary":
            metric_columns.extend(["Valid salaries", "Mean salary"])
        elif metric_key == "average_rating":
            metric_columns.extend(["Valid ratings", "Mean rating"])
        metric_rows = []
        for item in ranked:
            row = [item["department"], item["employee_count"]]
            if metric_key == "average_salary":
                row.extend([item["valid_salary_observations"], item["average_salary"]])
            elif metric_key == "average_rating":
                row.extend([item["valid_rating_observations"], item["average_rating"]])
            metric_rows.append(row)
        tables.append({
            "title": f"Department metrics{f' (top {_requested_top_n(question)})' if _requested_top_n(question) else ''} {'by original label' if raw_labels else 'after normalization'}",
            "columns": metric_columns,
            "rows": metric_rows,
        })
        if requested_chart_type(question) == "pie" and metric_key == "average_salary":
            chart_notice = "Department averages are not parts of a whole, so a sorted bar chart is more appropriate; I used one instead."
        if not raw_labels:
            limitations.append("Department labels marked Needs review are kept separate rather than merged using an unverified alias.")

    elif intent == "correlation_heatmap":
        pairs = dataset["salary_rating_pairs"]
        coefficient = None
        if len(pairs) >= 2:
            salaries = [pair[0] for pair in pairs]
            ratings = [pair[1] for pair in pairs]
            salary_mean = sum(salaries) / len(salaries)
            rating_mean = sum(ratings) / len(ratings)
            covariance = sum((salary - salary_mean) * (rating - rating_mean) for salary, rating in pairs)
            denominator = math.sqrt(
                sum((salary - salary_mean) ** 2 for salary in salaries)
                * sum((rating - rating_mean) ** 2 for rating in ratings)
            )
            if denominator:
                coefficient = max(-1.0, min(1.0, covariance / denominator))
        chart = make_chart("correlation_heatmap")
        if coefficient is None:
            answer = "A salary-rating correlation could not be estimated from the available valid paired observations."
            chart_notice = "Pearson correlation needs at least two paired valid observations and variation in both measures."
            insights = []
        else:
            answer = (
                f"Pearson correlation between salary and rating is {coefficient:.4f}, based on "
                f"{quality['valid_salary_rating_pairs']:,} eligible records with both measures valid."
            )
            insights = [
                f"The paired population is {quality['valid_salary_rating_pairs']:,} of {employee_count:,} unambiguous employees.",
                "Correlation describes association only; it does not establish that salary causes a rating change or vice versa.",
            ]
            if chart:
                tables.append({
                    "title": "Salary-rating correlation matrix",
                    "columns": ["Measure", *chart["x_labels"]],
                    "rows": [[label, *values] for label, values in zip(chart["y_labels"], chart["matrix"])],
                })
        limitations.append("Rating correlations require a documented scale; no rating values are included unless the scale is explicitly configured.")

    elif intent == "monthly_hiring":
        chart = make_chart("monthly_hiring")
        dates = dataset["valid_joining_dates"]
        if not dates and not (chart and chart.get("period")):
            answer = "No parseable joining dates are available for a monthly hiring trend."
            chart_notice = "A monthly hiring trend requires at least one parseable joining date."
            insights = []
        else:
            period = chart.get("period") if chart else None
            plotted_observations = chart.get("observations", 0) if chart else quality["valid_joining_dates"]
            outside_period = quality["valid_joining_dates"] - plotted_observations if period else 0
            missing_dates = quality["employee_missing_joining_date"]
            invalid_dates = quality["employee_unparseable_joining_dates"]
            ambiguous_dates = quality["employee_ambiguous_joining_dates"]
            period_text = f" for {period}" if period else " across the 12 months ending at the latest parseable date"
            answer = (
                f"The monthly hiring trend{period_text} includes {plotted_observations:,} unambiguous, parseable joining dates "
                f"from {employee_count:,} eligible records. Missing: {missing_dates:,}; invalid: {invalid_dates:,}; "
                f"ambiguous: {ambiguous_dates:,}; outside the requested period: {outside_period:,}."
            )
            insights = [
                f"The chart shows {len(chart['labels']) if chart else 0} chronological months with zero-count months retained.",
                f"{plotted_observations:,} eligible employee joining dates fall within the plotted period.",
            ]
            if chart:
                tables.append({
                    "title": "Monthly hires shown in chart",
                    "columns": ["Month", "Hires"],
                    "rows": [[label, count] for label, count in zip(chart["labels"], chart["values"])],
                })
        limitations.append("The trend uses unambiguous parseable dates only and displays the 12 calendar months ending at the latest valid date.")

    else:
        paired = quality["valid_salary_rating_pairs"]
        salary_summary = (
            f"mean salary is {salary_average:,.2f} (n={quality['valid_salary_observations']:,})"
            if salary_average is not None else "no valid salary observations are available"
        )
        rating_summary = (
            f"mean rating is {rating_average:.2f} (n={quality['valid_rating_observations']:,})"
            if rating_average is not None else "rating was not assessed because the scale is not configured"
        )
        pair_summary = f"{paired:,} records have both values valid" if paired is not None else "paired rating measures are not assessed because the scale is not configured"
        answer = f"Across eligible records, {salary_summary} and {rating_summary}; {pair_summary}."
        insights = [
            f"Valid salary observations: {quality['valid_salary_observations']:,} of {employee_count:,} eligible records.",
            f"Numeric rating observations: {quality['numeric_rating_observations']:,}; scale validation is {'configured' if quality['rating_scale'] else 'not configured'}.",
            "Salary averages use valid employee observations; rating averages require a documented scale.",
        ]
        tables.append({
            "title": "Overall salary and rating",
            "columns": ["Measure", "Average", "Record observations"],
            "rows": [
                ["Salary", salary_average, quality["valid_salary_observations"]],
                [f"Rating ({rating_scale_label})", rating_average if rating_average is not None else "Not assessed: scale not configured", rating_count_label],
                ["Records with both measures", "Not an average", pair_count_label],
            ],
        })
        department_rows = [[
            item["department"], item["employee_count"], item["valid_salary_observations"],
            item["average_salary"], item["valid_rating_observations"], item["average_rating"],
        ] for item in dataset["department_metrics"]]
        tables.append({
            "title": "Department metrics after normalization",
            "columns": ["Department", "Eligible records", "Valid salaries", "Mean salary", "Valid ratings", "Mean rating"],
            "rows": department_rows,
        })
        limitations.append("Department labels marked Needs review are kept separate, so their department-level averages remain provisional.")
        if not quality["rating_scale"]:
            limitations.append("Rating scale is not configured; rating averages, correlations, and out-of-range classifications are withheld.")

    if chart_failure_type:
        chart_notice = "The analysis is ready, but the requested chart could not be generated."
        limitations.append(chart_notice)

    analysis = {
        "answer": answer,
        "insights": insights[:3],
        "possible_explanations": [],
        "implications": [],
        "recommendations": recommendations if intent == "data_quality" else [],
        "conclusion": answer,
        "tables": tables,
        "limitations": limitations,
        "data_quality": quality,
    }
    if chart_failure_type:
        analysis["_chart_error_type"] = chart_failure_type
    if intent == "correlation_heatmap" and chart:
        limitations.append("Correlation is an association measure and does not establish causation.")
    return analysis, chart, chart_notice


def build_workforce_report(question, dataset):
    quality = dataset["quality"]
    top_n = _requested_top_n(question)
    if employee_analysis_intent(question) == "employee_salary_ranking":
        analysis, _, _ = build_employee_analysis(question, "employee_salary_ranking", dataset)
        chart = build_employee_chart(dataset, "employee_salary_ranking", question)
        rows = analysis["tables"][0]["rows"]
        charts = [{
            "chart": chart,
            "interpretation": "Bars compare positive finite salaries for distinct eligible employee IDs; records under reused IDs were excluded.",
        }] if chart and rows else []
        report = {
            "title": f"Top {len(rows)} Highest-Paid Employees Report",
            "question": question,
            "business_objective": "Compare the highest validated employee salary records requested.",
            "scope": f"The ranking shows {len(rows)} employees from {quality['employee_rows_used_for_metrics']:,} eligible records.",
            "data_source": "The complete employees_messy source table, read without modifying source data.",
            "methodology": "Exact duplicate rows were removed in memory. Every row under a reused employee ID was excluded. Only positive finite salaries were ranked; no currency was configured.",
            "limitations": analysis["limitations"] + [
                f"{quality['valid_salary_observations']:,} positive finite salaries were available from {quality['employee_rows_used_for_metrics']:,} eligible records."
            ],
            "tables": analysis["tables"],
            "answer": analysis["answer"],
            "insights": analysis["insights"],
            "recommendations": ["Review role and location mix before interpreting salary differences as compensation inequity."],
            "possible_explanations": [],
            "implications": [],
            "conclusion": analysis["answer"],
            "charts": charts,
            "truncated": dataset["truncated"],
        }
        if re.search(r"\bdashboard\b", question, re.IGNORECASE):
            report["dashboard"] = {"kpis": [], "findings": analysis["insights"], "actions": report["recommendations"]}
        return report, analysis

    top_salary_request = bool(
        top_n
        and re.search(r"\bdepartments?\b", question, re.IGNORECASE)
        and re.search(r"\b(?:average|avg|mean)\b", question, re.IGNORECASE)
        and re.search(r"\b(?:salary|pay|compensation)\b", question, re.IGNORECASE)
    )
    if top_salary_request:
        analysis, chart, chart_notice = build_employee_analysis(question, "department_metrics", dataset)
        metric_rows = analysis["tables"][0]["rows"]
        department_count = len(metric_rows)
        top_item, low_item = metric_rows[0], metric_rows[-1]
        spread = float(top_item[3]) - float(low_item[3]) if department_count > 1 else 0.0
        findings = [
            f"{top_item[0]} has the highest displayed average salary at {float(top_item[3]):,.2f}, based on {int(top_item[2]):,} valid records.",
        ]
        if department_count > 1:
            findings.append(
                f"The displayed top-{department_count} range is {spread:,.2f}, from {top_item[0]} to {low_item[0]}."
            )
        findings.append(
            f"The comparison uses {quality['valid_salary_observations']:,} positive finite salary records from "
            f"{quality['employee_rows_used_for_metrics']:,} eligible source records."
        )
        recommendations = [
            f"Review role and location mix across {top_item[0]} and {low_item[0]} before interpreting the observed average-salary difference or changing compensation."
        ] if department_count > 1 else [
            f"Review the role and location mix in {top_item[0]} before interpreting its average salary."
        ]
        reconciliation = _record_reconciliation_table(quality)
        if chart:
            chart["title"] = f"Top {department_count} departments by average salary"
        salary_values = [float(row[3]) for row in metric_rows]
        average_of_displayed = sum(salary_values) / len(salary_values) if salary_values else None
        kpis = [
            {"label": "Departments shown", "value": f"{department_count:,}"},
            {"label": "Records analyzed", "value": f"{quality['employee_rows_used_for_metrics']:,}"},
            {"label": "Valid salary records", "value": f"{quality['valid_salary_observations']:,}"},
            {"label": "Mean of displayed department averages", "value": f"{average_of_displayed:,.2f}" if average_of_displayed is not None else "Unavailable"},
        ]
        scope = (
            f"The full {quality['source_rows']:,}-row source was checked; the chart and department table show the top "
            f"{department_count} eligible department averages."
        )
        report = {
            "title": f"Top {department_count} Department Average Salary Report",
            "question": question,
            "business_objective": "Compare the highest department average salaries using valid source records.",
            "scope": scope,
            "data_source": "The complete employees_messy source table, read without modifying source data.",
            "methodology": (
                "Exact duplicate rows were removed in memory. Every distinct row under a reused ID was excluded; "
                "no latest-record rule was inferred. Remaining rows are eligible records, not verified unique people. "
                "Department aliases explicitly listed in the mapping are consolidated; uncertain labels remain separate. "
                "Department salary means use finite positive salary values only. Values are in source units; no currency was configured."
            ),
            "limitations": [
                f"{quality['records_needing_department_standardization']:,} records needed department normalization or review; uncertain labels were not guessed.",
                f"The source contains {quality['negative_salary_values']:,} negative and {quality['zero_salary_values']:,} zero salaries; "
                f"{quality['valid_salary_observations']:,} valid positive finite values were used in salary metrics.",
                "No rating analysis was requested; rating scale configuration is not assumed.",
            ],
            "tables": [*analysis["tables"], reconciliation],
            "answer": analysis["answer"],
            "insights": findings,
            "recommendations": recommendations,
            "possible_explanations": [],
            "implications": [
                "The averages are descriptive; compare job mix and location before treating department differences as evidence for a compensation change."
            ],
            "conclusion": analysis["answer"],
            "charts": [{
                "chart": chart,
                "interpretation": "Sorted from highest to lowest. Each bar is the average of finite positive salary records in that department; values are in source units, not a specified currency.",
            }] if chart else [],
            "chart_notice": chart_notice,
            "truncated": dataset["truncated"],
        }
        if re.search(r"\bdashboard\b", question, re.IGNORECASE):
            report["dashboard"] = {"kpis": kpis, "findings": findings, "actions": recommendations}
        return report, analysis

    if employee_analysis_intent(question) == "department_metrics" and re.search(r"\bdepartments?\b", question, re.IGNORECASE):
        analysis, chart, chart_notice = build_employee_analysis(question, "department_metrics", dataset)
        metric_title = analysis["tables"][0]["title"] if analysis["tables"] else "Department comparison"
        report = {
            "title": metric_title,
            "question": question,
            "business_objective": "Answer the requested department comparison using eligible source records.",
            "scope": f"Department metrics use {quality['employee_rows_used_for_metrics']:,} eligible records from {quality['source_rows']:,} source rows.",
            "data_source": "The complete employees_messy source table, read without modifying source data.",
            "methodology": "Exact duplicates were removed in memory; all records under reused employee IDs were excluded. Salary averages use positive finite values only. Unmapped department labels remain separate for review.",
            "limitations": analysis["limitations"],
            "tables": analysis["tables"],
            "answer": analysis["answer"],
            "insights": analysis["insights"],
            "recommendations": analysis["recommendations"],
            "possible_explanations": [],
            "implications": [],
            "conclusion": analysis["answer"],
            "charts": [{"chart": chart, "interpretation": "The chart compares the requested department metric using eligible records."}] if chart else [],
            "chart_notice": chart_notice,
            "truncated": dataset["truncated"],
        }
        if re.search(r"\bdashboard\b", question, re.IGNORECASE):
            report["dashboard"] = {"kpis": [], "findings": analysis["insights"], "actions": analysis["recommendations"]}
        return report, analysis

    analysis, _, _ = build_employee_analysis(question, "salary_rating", dataset)
    charts = []

    def add_chart(chart, interpretation):
        if chart and chart.get("type"):
            charts.append({"chart": chart, "interpretation": interpretation})

    department_rows = dataset["department_metrics"]
    explicit_chart = bool(re.search(r"\b(?:chart|graph|plot|visuali[sz](?:e|ation))\b", question, re.IGNORECASE))
    chart_request = requested_chart_type(question)
    broad_report = not explicit_chart
    headcount_result = {
        "columns": ["department", "record_count"],
        "rows": [[item["department"], item["employee_count"]] for item in department_rows],
    }
    headcount_chart = build_chart(headcount_result, chart_request if explicit_chart and not re.search(r"\b(?:salary|pay|compensation|rating)\b", question, re.IGNORECASE) else "bar") if broad_report or (explicit_chart and chart_request not in {"histogram", "box", "scatter", "heatmap", "line"} and not re.search(r"\b(?:salary|pay|compensation|rating)\b", question, re.IGNORECASE)) else None
    if headcount_chart:
        headcount_chart["title"] = "Eligible records by department"
        add_chart(headcount_chart, "Bars count records with one distinct source row per nonmissing ID. An ID does not verify a unique person. Known department aliases are consolidated; uncertain labels remain separate.")

    salary_rows = [[item["department"], item["average_salary"]]
                   for item in department_rows if item["average_salary"] is not None]
    salary_chart = build_chart({
        "columns": ["department", "average_salary"],
        "rows": salary_rows,
    }, chart_request if explicit_chart else "bar") if broad_report or (explicit_chart and re.search(r"\b(?:salary|pay|compensation)\b", question, re.IGNORECASE) and chart_request not in {"histogram", "box", "scatter", "heatmap", "line"}) else None
    if salary_chart:
        salary_chart["title"] = "Average salary by department"
        add_chart(salary_chart, "Department averages use positive finite employee salaries only. The valid-observation count for each department is shown in the report table.")

    rating_rows = [[item["department"], item["average_rating"]]
                   for item in department_rows if item["average_rating"] is not None]
    rating_chart = None
    if explicit_chart and re.search(r"\brating\b", question, re.IGNORECASE) and chart_request not in {"histogram", "box", "scatter", "heatmap", "line"}:
        rating_chart = build_chart({"columns": ["department", "average_rating"], "rows": rating_rows}, chart_request or "bar")
        if rating_chart:
            rating_chart["title"] = "Average rating by department"
            add_chart(rating_chart, "Department averages use ratings within the explicitly configured scale. Each department's valid rating count is reported separately.")

    if explicit_chart and chart_request in {"histogram", "box", "scatter", "heatmap", "line"}:
        chart_kind = {"box": "salary_box", "scatter": "scatter", "heatmap": "correlation_heatmap", "line": "monthly_hiring"}.get(chart_request, chart_request)
        specialized_chart = build_employee_chart(dataset, chart_kind, question)
        if specialized_chart:
            add_chart(specialized_chart, "This chart uses validated observations for the requested distribution, relationship, or trend.")

    department_summary = "; ".join(
        f"{item['department']}: {item['employee_count']} records"
        for item in sorted(department_rows, key=lambda item: item["employee_count"], reverse=True)
    )
    findings = [
        f"The analysis uses {quality['employee_rows_used_for_metrics']:,} eligible records from {quality['source_rows']:,} source rows; IDs are not verified unique people.",
        f"{quality['exact_duplicate_rows']:,} exact duplicate extra rows and {quality['rows_with_reused_employee_ids']:,} rows under reused IDs were excluded from employee-level metrics.",
    ]
    salary_groups = _rank_department_metrics("", department_rows, "average_salary")
    if salary_groups:
        top_salary = salary_groups[0]
        low_salary = salary_groups[-1]
        findings.append(
            f"Average salary ranges from {top_salary['average_salary']:,.2f} in {top_salary['department']} "
            f"to {low_salary['average_salary']:,.2f} in {low_salary['department']} across eligible groups."
        )
    recommendations = [
        "Review IDs that map to multiple distinct source records before assigning a canonical history; this report does not choose a latest row."
    ] if quality["duplicate_employee_id_count"] else []
    if quality["department_mapping_review_counts"]:
        recommendations.append("Review missing or unmapped department labels; they remain separate rather than being assigned to a guessed department.")
    if quality["negative_salary_values"] or quality["zero_salary_values"] or quality["invalid_salary_values"]:
        recommendations.append("Validate negative, zero, and nonnumeric salary entries with the source owner; these values were excluded from salary averages without changing source data.")
    if quality["out_of_range_rating_values"] or quality["invalid_rating_values"]:
        recommendations.append("Confirm the documented rating scale and review values outside it before using them in performance comparisons.")
    if quality["rating_scale"] is None:
        recommendations.append("Confirm and configure the documented rating scale before comparing rating averages or excluding numeric values.")
    if not recommendations:
        recommendations.append("Monitor department headcount and validate metric coverage against the reporting period before making staffing changes.")

    average_salary = quality["average_salary"]
    average_rating = quality["average_rating"]
    kpis = [
        {"label": "Records analyzed", "value": f"{quality['employee_rows_used_for_metrics']:,}"},
        {"label": "Departments", "value": f"{len(department_rows):,}"},
        {"label": "Average salary", "value": f"{average_salary:,.2f}" if average_salary is not None else "Unavailable"},
        {"label": "Average rating", "value": f"{average_rating:.2f}" if average_rating is not None else "Not assessed: scale not configured"},
        {"label": "Valid salaries", "value": f"{quality['valid_salary_observations']:,}"},
        {"label": "Valid ratings", "value": f"{quality['valid_rating_observations']:,}" if quality["valid_rating_observations"] is not None else "Not assessed"},
    ]
    analysis["answer"] = (
        f"Department headcount: {department_summary}. "
        f"Across {quality['employee_rows_used_for_metrics']:,} eligible records, "
        f"average salary is {average_salary:,.2f} and average rating is {average_rating:.2f} "
        "among validated observations."
        if average_salary is not None and average_rating is not None
        else (
            f"Department record counts: {department_summary}. Average salary is "
            f"{average_salary:,.2f} across valid records; rating was not assessed because the scale is not configured."
            if average_salary is not None and quality["rating_scale"] is None
            else f"Department record counts: {department_summary}. Some requested measures have no valid observations."
        )
    )
    analysis["conclusion"] = analysis["answer"]
    analysis["insights"] = findings[:3]
    analysis["recommendations"] = recommendations[:4]
    report = {
        "title": "Employee Workforce Report",
        "question": question,
        "business_objective": "Summarize the employee and department measures supported by the complete source dataset.",
        "scope": f"Complete source query: {quality['source_rows']:,} rows; metrics use {quality['employee_rows_used_for_metrics']:,} eligible records after ID-conflict and missing-ID exclusions.",
        "data_source": "The complete employees_messy source table, read without modifying source data.",
        "methodology": (
            "Exact duplicate rows were counted then removed in memory. All records under employee IDs with multiple differing rows were excluded from employee metrics. Department aliases explicitly listed in the mapping are consolidated; unknown and missing labels remain separate. "
            f"Salary observations require finite values greater than zero. Rating metrics are included only within an explicitly configured scale. Dates are parsed without altering source values."
        ),
        "limitations": [
            f"{quality['records_needing_department_standardization']:,} records needed department normalization or review; {sum(quality['department_mapping_review_counts'].values()):,} uncertain/missing labels were not guessed.",
            f"Source-row salary anomalies: {quality['negative_salary_values']:,} negative, {quality['zero_salary_values']:,} zero, and {quality['invalid_salary_values']:,} nonnumeric values. "
            + (f"Rating values outside the configured scale: {quality['out_of_range_rating_values']:,}." if quality["rating_scale"] else "Rating scale is not configured; observed values are not classified as valid or out of range."),
        ],
        "tables": [*analysis["tables"], _record_reconciliation_table(quality)],
        "answer": analysis["answer"],
        "insights": findings,
        "recommendations": recommendations[:4],
        "possible_explanations": [],
        "implications": [
            f"Department average salaries span {salary_groups[-1]['average_salary']:,.2f} to {salary_groups[0]['average_salary']:,.2f}; compare job and location mix before interpreting this descriptive spread."
        ] if salary_groups else [],
        "conclusion": analysis["answer"],
        "charts": charts,
        "truncated": dataset["truncated"],
    }
    if len(charts) > 1 or re.search(r"\bdashboard\b", question, re.IGNORECASE):
        report["dashboard"] = {"kpis": kpis, "findings": findings, "actions": recommendations[:4]}
    return report, analysis


def build_query_report(question, result, answer):
    columns = list(result.get("columns", []))
    rows = [list(row) for row in result.get("rows", [])]
    chart_columns, chart_rows = safe_analysis_rows(result, limit=500)
    chart = None
    chart_notice = None
    requested_type = requested_chart_type(question)
    if requested_type:
        chart = build_chart({"columns": chart_columns, "rows": chart_rows, "question": question}, requested_type)
        if chart is None:
            alternative = build_chart({"columns": chart_columns, "rows": chart_rows, "question": question})
            if alternative:
                chart = alternative
            chart_notice = chart_unavailable_message(requested_type, {"columns": chart_columns, "rows": chart_rows}, chart.get("type") if chart else None)
    elif len(chart_columns) >= 2 and len(chart_rows) >= 2:
        comparison_requested = bool(re.search(r"\b(?:by|compare|comparison|versus|vs\.?|across)\b", question or "", re.IGNORECASE))
        metric_indices = [
            index for index, column in enumerate(chart_columns)
            if NUMERIC_COLUMN.search(str(column))
            or sum(_numeric_value(row[index]) is not None for row in chart_rows) >= max(2, math.ceil(len(chart_rows) * 0.8))
        ]
        category_indices = [
            index for index in range(len(columns))
            if index not in metric_indices and index < len(chart_columns)
            and not DATE_COLUMN.search(str(chart_columns[index]))
        ]
        if comparison_requested and metric_indices and category_indices:
            chart = build_chart({"columns": chart_columns, "rows": chart_rows, "question": question}, "bar")
        elif re.search(r"\b(?:trend|over time|monthly|weekly|yearly)\b", question or "", re.IGNORECASE):
            date_indices = [index for index, column in enumerate(chart_columns) if DATE_COLUMN.search(str(column))]
            if date_indices and metric_indices:
                chart = build_chart({"columns": chart_columns, "rows": chart_rows, "question": question}, "line")
        elif re.search(r"\bdistribution\b", question or "", re.IGNORECASE) and len(metric_indices) == 1:
            chart = build_chart({"columns": chart_columns, "rows": chart_rows, "question": question}, "histogram")
    title = " ".join(str(question or "Employee query").split())[:120]
    report = {
        "title": title[:1].upper() + title[1:] + " Report",
        "question": question,
        "business_objective": "Provide the requested result using the returned employee dataset rows.",
        "scope": f"The report contains {len(rows):,} returned result rows.",
        "data_source": "Read-only query results from the employees_messy source table.",
        "methodology": "The report table reproduces every returned query row and column. Automatically generated charts omit employee identifiers and contact fields.",
        "limitations": ["The result table is limited to the requested query output."],
        "tables": [{"title": "Requested results", "columns": columns, "rows": rows}] if columns and rows else [],
        "answer": answer or "The requested query completed successfully.",
        "insights": [],
        "recommendations": [],
        "possible_explanations": [],
        "implications": [],
        "conclusion": answer or "The requested query completed successfully.",
        "charts": [{"chart": chart, "interpretation": "The chart visualizes the returned query results."}] if chart else [],
        "chart_notice": chart_notice,
        "truncated": bool(result.get("truncated", False)),
    }
    if re.search(r"\bdashboard\b", question or "", re.IGNORECASE):
        report["dashboard"] = {"kpis": [{"label": "Rows returned", "value": f"{len(rows):,}"}], "findings": [], "actions": []}
    return report, {"answer": report["answer"], "tables": report["tables"], "insights": [], "limitations": report["limitations"]}


def build_analysis_report(question, analysis, chart=None):
    title = " ".join(str(question or "Employee analysis").split())[:120]
    answer = analysis.get("answer") or "The requested employee analysis is complete."
    quality = analysis.get("data_quality") or {}
    source_rows = quality.get("source_rows")
    report = {
        "title": title[:1].upper() + title[1:] + " Report",
        "question": question,
        "business_objective": "Answer the requested focused employee-data analysis.",
        "scope": (
            f"Analysis uses {source_rows:,} source rows."
            if isinstance(source_rows, int)
            else "The report contains the requested analysis results."
        ),
        "data_source": "The complete employees_messy source table, read without modifying source data.",
        "methodology": "The report uses the same computed analysis, population rules, result tables, and examples shown in the chat response.",
        "limitations": list(analysis.get("limitations", [])),
        "tables": [
            {"title": table.get("title", "Requested results"), "columns": list(table["columns"]), "rows": list(table["rows"])}
            for table in analysis.get("tables", [])
            if table.get("columns") and table.get("rows") is not None
        ],
        "answer": answer,
        "insights": list(analysis.get("insights", [])),
        "recommendations": list(analysis.get("recommendations", [])),
        "possible_explanations": list(analysis.get("possible_explanations", [])),
        "implications": list(analysis.get("implications", [])),
        "conclusion": analysis.get("conclusion") or answer,
        "chart": chart if isinstance(chart, dict) else {},
        "truncated": False,
    }
    if re.search(r"\bdashboard\b", question or "", re.IGNORECASE):
        report["dashboard"] = {
            "kpis": ([{"label": "Source rows", "value": f"{source_rows:,}"}] if isinstance(source_rows, int) else []),
            "findings": report["insights"][:4],
            "actions": report["recommendations"][:4],
        }
    return report, analysis


def direct_department_answer(question, dataset):
    text = str(question or "").casefold()
    if re.search(r"\b(?:salary|pay|compensation)\b", text):
        value_key = "average_salary"
        value_label = "average salary"
    elif re.search(r"\b(?:rating|performance score)\b", text):
        if dataset["quality"]["rating_scale"] is None:
            minimum = dataset["quality"]["numeric_rating_minimum"]
            maximum = dataset["quality"]["numeric_rating_maximum"]
            observed = f" Observed numeric values span {minimum} to {maximum}." if minimum is not None else ""
            return "The rating scale is not configured, so I can't compare or exclude rating values yet." + observed
        value_key = "average_rating"
        value_label = "average rating"
    else:
        value_key = "employee_count"
        value_label = "employees"

    entries = []
    raw_labels = original_department_labels_requested(question)
    department_metrics = dataset["raw_department_metrics"] if raw_labels else dataset["department_metrics"]
    for item in department_metrics:
        value = item.get(value_key)
        if value is None:
            continue
        if value_key == "average_salary":
            formatted = f"{value:,.2f} (n={item['valid_salary_observations']})"
        elif value_key == "average_rating":
            formatted = f"{value:.2f} (n={item['valid_rating_observations']})"
        else:
            formatted = f"{value:,}"
        entries.append((item["department"], value, formatted))
    entries.sort(key=lambda entry: entry[1], reverse=True)
    top_n = _requested_top_n(question)
    if top_n is not None:
        entries = entries[:top_n]
    if not entries:
        return f"No valid {value_label} values are available by department."
    prefix = f"Top {len(entries)} department {value_label}: " if top_n is not None else f"Department {value_label}: "
    answer = prefix + "; ".join(
        f"{department}: {formatted}" for department, _, formatted in entries
    ) + "."
    if not raw_labels and dataset["quality"]["records_needing_department_standardization"]:
        answer += " Known department aliases are consolidated; unmapped labels remain separate for review."
    return answer


def employee_analysis_intent(question):
    text = str(question or "").casefold()
    if "895" in text and "492" in text:
        return "count_reconciliation"
    if re.search(r"\b(?:join(?:ing)?|hire|hiring)[\s_-]*date\b", text) and re.search(
        r"\b(?:diagnostic\w*|quality|valid|missing|invalid|ambiguous|example\w*|classif\w*)\b", text
    ):
        return "date_diagnostics"
    if re.search(r"\bmissing\b", text) and re.search(
        r"\b(?:email|phone|mobile|telephone|name|department|dept|salary|pay|join[_ ]?date|hire[_ ]?date|city|rating|status)\b",
        text,
    ):
        return "missing_values"
    if re.search(r"\b(data quality|quality check|missing values?|duplicate rows?|standardiz\w+)\b", text):
        return "data_quality"
    if "histogram" in text:
        return "histogram"
    if re.search(r"\bbox\s*[- ]?plots?\b|\bboxplots?\b", text):
        return "salary_box"
    if "scatter" in text and re.search(r"\b(salary|pay)\b", text) and re.search(r"\b(rating|performance)\b", text):
        return "employee_scatter"
    if (
        re.search(r"\b(?:top|highest|best[- ]paid|highest[- ]earning)\b", text)
        and re.search(r"\b(?:employees?|earners?|staff|people)\b", text)
        and re.search(
            r"\b(?:in|per|by)\s+(?:each|every)\s+(?:(?:normalized|raw|original|canonical)\s+)*(?:department|dept)\b|"
            r"\b(?:department|dept)[- ]wise\b",
            text,
        )
        and re.search(r"\b(?:salary|pay|earning|paid|compensation)\b", text)
    ):
        return "department_employee_salary_ranking"
    if (
        re.search(r"\b(?:top|highest|best[- ]paid|highest[- ]earning)\b", text)
        and re.search(r"\b(?:employees?|earners?|staff|people)\b", text)
        and re.search(r"\b(?:salary|pay|earning|paid|compensation)\b", text)
    ):
        return "employee_salary_ranking"
    if (
        re.search(r"\b(?:employees?|staff|people|workers)\b", text)
        and re.search(r"\b(?:department|dept)\b", text)
        and re.search(r"\b(?:salary|salaries|pay|compensation)\b", text)
        and re.search(r"\b(?:average|mean)\b", text)
        and re.search(r"\b(?:above|exceed\w*|higher|greater|more than|over)\b", text)
    ):
        return "employees_above_department_salary_average"
    if "correlat" in text and ("heatmap" in text or "heat map" in text):
        return "correlation_heatmap"
    if ("monthly" in text or "month-by-month" in text) and re.search(r"\b(hir\w*|join\w*|employ\w*)\b", text):
        return "monthly_hiring"
    if "department" in text and re.search(r"\b(employee|headcount|count|salary|rating|pay)\w*\b", text):
        return "department_metrics"
    if "salary" in text and "rating" in text and re.search(r"\b(average|averages|compare|comparison|relationship|correlat)\b", text):
        return "salary_rating"
    return None


def safe_analysis_rows(result, limit=80):
    columns = result.get("columns", [])
    visible_indexes = [index for index, column in enumerate(columns) if not is_sensitive_column(column)]
    visible_columns = [columns[index] for index in visible_indexes]
    rows = [
        [safe_analysis_value(columns[index], row[index]) if index < len(row) else None for index in visible_indexes]
        for row in result.get("rows", [])[:limit]
    ]
    return visible_columns, rows


def build_chart(result, requested_type=None):
    columns = result.get("columns", [])
    rows = result.get("rows", [])

    numeric_indexes = []
    category_indexes = []
    for index, column in enumerate(columns):
        normalized = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
        if is_sensitive_column(column) or ID_COLUMN.search(normalized):
            continue
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        if not values:
            continue
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            numeric_indexes.append(index)
        elif all(isinstance(value, str) for value in values):
            category_indexes.append(index)
    requested_type = requested_type or requested_chart_type(result.get("question", ""))
    numeric_values = {
        index: [float(value) for row in rows if index < len(row)
                and isinstance((value := row[index]), (int, float))
                and not isinstance(value, bool) and math.isfinite(float(value))]
        for index in numeric_indexes
    }
    numeric_indexes = [index for index in numeric_indexes if numeric_values[index]]
    date_indexes = [index for index in category_indexes if DATE_COLUMN.search(str(columns[index]))]
    default_metric = numeric_indexes[0] if numeric_indexes else None

    def name(index):
        return re.sub(r"[_\s]+", " ", str(columns[index])).strip().title()

    def base(chart_type, title, labels, values, metric_index=None, dimension_index=None, **extra):
        metric = name(metric_index) if metric_index is not None else "Frequency"
        dimension = name(dimension_index) if dimension_index is not None else metric
        unit = _chart_unit(metric)
        return {
            "type": chart_type,
            "title": title[:100],
            "metric": metric,
            "dimension": dimension,
            "unit": unit,
            "labels": labels,
            "values": values,
            **extra,
        }

    def grouped_values(dimension_index, metric_index):
        totals = OrderedDict()
        for row in rows:
            if dimension_index >= len(row) or metric_index >= len(row):
                continue
            label = safe_analysis_value(columns[dimension_index], row[dimension_index])
            if str(columns[dimension_index]).strip().casefold() == "department":
                label = normalize_department(label)[0]
            value = row[metric_index]
            if label is None or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                continue
            label = str(label).strip()
            totals[label] = totals.get(label, 0.0) + float(value)
        return totals

    def numeric_column(index):
        return numeric_values.get(index, [])

    if requested_type == "scatter":
        if len(numeric_indexes) < 2 or len(rows) < 3:
            return None
        x_index, y_index = numeric_indexes[:2]
        points = [
            {"x": float(row[x_index]), "y": float(row[y_index])}
            for row in rows
            if len(row) > max(x_index, y_index)
            and isinstance(row[x_index], (int, float)) and math.isfinite(float(row[x_index]))
            and isinstance(row[y_index], (int, float)) and math.isfinite(float(row[y_index]))
        ]
        if len(points) < 3:
            return None
        return base("scatter", f"{name(y_index)} vs. {name(x_index)}", [], [], y_index,
                    x_label=name(x_index), y_label=name(y_index), x_unit=_chart_unit(name(x_index)),
                    y_unit=_chart_unit(name(y_index)), points=points[:300])

    if requested_type is None and not category_indexes and len(numeric_indexes) >= 2 and len(rows) >= 3:
        return build_chart(result, "scatter")

    if requested_type == "histogram" or (requested_type is None and not category_indexes and default_metric is not None):
        if default_metric is None or not numeric_column(default_metric):
            return None
        values = numeric_column(default_metric)
        minimum, maximum = min(values), max(values)
        salary_metric = "salary" in str(columns[default_metric]).casefold()
        format_bin = (lambda value: f"{value:,.2f}") if salary_metric else (lambda value: f"{value:,.3g}")
        if minimum == maximum:
            chart = base("histogram", f"Distribution of {name(default_metric)}", [format_bin(minimum)], [len(values)],
                         default_metric, bin_edges=[minimum, maximum], x_label=name(default_metric),
                         y_label="Number of records")
            chart["unit"] = "records"
            chart["x_unit"] = _chart_unit(name(default_metric))
            return chart
        bin_count = min(10, max(4, math.ceil(math.sqrt(len(values)))))
        bin_width = (maximum - minimum) / bin_count
        bins = [0] * bin_count
        for value in values:
            bins[min(int((value - minimum) / bin_width), bin_count - 1)] += 1
        labels = [
            f"{format_bin(minimum + index * bin_width)}–{format_bin(minimum + (index + 1) * bin_width)}"
            for index in range(bin_count)
        ]
        chart = base("histogram", f"Distribution of {name(default_metric)}", labels, bins,
                 default_metric, bin_edges=[round(minimum + index * bin_width, 4) for index in range(bin_count + 1)],
                 x_label=name(default_metric), y_label="Number of records")
        chart["unit"] = "records"
        chart["x_unit"] = _chart_unit(name(default_metric))
        return chart

    if requested_type == "box":
        if default_metric is None or not numeric_column(default_metric):
            return None
        candidate_categories = [index for index in category_indexes if index not in date_indexes]
        groups = OrderedDict()
        if candidate_categories:
            category_index = candidate_categories[0]
            for row in rows:
                if len(row) <= max(default_metric, category_index):
                    continue
                label = safe_analysis_value(columns[category_index], row[category_index])
                value = row[default_metric]
                if label is not None and isinstance(value, (int, float)) and math.isfinite(float(value)):
                    groups.setdefault(str(label), []).append(float(value))
        else:
            groups["All results"] = numeric_column(default_metric)
        groups = OrderedDict((label, values) for label, values in groups.items() if values)
        if not groups:
            return None
        summaries = []
        for label, values in groups.items():
            ordered = sorted(values)
            first_quartile = _percentile(ordered, 0.25)
            third_quartile = _percentile(ordered, 0.75)
            interquartile_range = third_quartile - first_quartile
            lower_fence = first_quartile - 1.5 * interquartile_range
            upper_fence = third_quartile + 1.5 * interquartile_range
            inlier_values = [value for value in ordered if lower_fence <= value <= upper_fence]
            summaries.append({
                "label": label,
                "min": inlier_values[0],
                "q1": first_quartile,
                "median": _percentile(ordered, 0.5),
                "q3": third_quartile,
                "max": inlier_values[-1],
                "outliers": [value for value in ordered if value < lower_fence or value > upper_fence],
            })
        return base("box", f"Distribution of {name(default_metric)}", [], [], default_metric,
                    groups=summaries, x_label=name(default_metric), y_label="Group")

    if requested_type == "heatmap":
        question = str(result.get("question", "")).casefold()
        if "correlat" in question and len(numeric_indexes) >= 2:
            labels = [name(index) for index in numeric_indexes[:10]]
            matrix = []
            for y_index in numeric_indexes[:10]:
                matrix_row = []
                for x_index in numeric_indexes[:10]:
                    pairs = [
                        (float(row[x_index]), float(row[y_index]))
                        for row in rows
                        if len(row) > max(x_index, y_index)
                        and isinstance(row[x_index], (int, float)) and not isinstance(row[x_index], bool)
                        and isinstance(row[y_index], (int, float)) and not isinstance(row[y_index], bool)
                        and math.isfinite(float(row[x_index])) and math.isfinite(float(row[y_index]))
                    ]
                    if len(pairs) < 2:
                        return None
                    x_values = [pair[0] for pair in pairs]
                    y_values = [pair[1] for pair in pairs]
                    x_mean = sum(x_values) / len(x_values)
                    y_mean = sum(y_values) / len(y_values)
                    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
                    denominator = math.sqrt(
                        sum((x - x_mean) ** 2 for x in x_values)
                        * sum((y - y_mean) ** 2 for y in y_values)
                    )
                    if denominator == 0:
                        return None
                    matrix_row.append(round(max(-1.0, min(1.0, numerator / denominator)), 4))
                matrix.append(matrix_row)
            return base("heatmap", "Numeric measure correlation heatmap", [], [], numeric_indexes[0],
                        x_labels=labels, y_labels=labels, matrix=matrix,
                        x_label="Measure", y_label="Measure", unit="r")
        if len(category_indexes) < 2 or default_metric is None:
            return None
        x_index, y_index = category_indexes[:2]
        x_labels = []
        y_labels = []
        cells = {}
        for row in rows:
            if len(row) <= max(x_index, y_index, default_metric):
                continue
            x_label = safe_analysis_value(columns[x_index], row[x_index])
            y_label = safe_analysis_value(columns[y_index], row[y_index])
            value = row[default_metric]
            if x_label is None or y_label is None or not isinstance(value, (int, float)):
                continue
            x_label, y_label = str(x_label)[:32], str(y_label)[:32]
            if x_label not in x_labels:
                x_labels.append(x_label)
            if y_label not in y_labels:
                y_labels.append(y_label)
            cells[(y_label, x_label)] = cells.get((y_label, x_label), 0.0) + float(value)
        if len(x_labels) < 2 or len(y_labels) < 2:
            return None
        return base("heatmap", f"{name(default_metric)} by {name(y_index)} and {name(x_index)}", [], [], default_metric,
                    x_labels=x_labels, y_labels=y_labels,
                    matrix=[[round(cells.get((y, x), 0.0), 4) for x in x_labels] for y in y_labels],
                    x_label=name(x_index), y_label=name(y_index))

    if not numeric_indexes or not category_indexes:
        if requested_type is None and len(numeric_indexes) >= 2 and len(rows) >= 3:
            return build_chart(result, "scatter")
        return None

    dimension_index = date_indexes[0] if date_indexes else category_indexes[0]
    metric_index = default_metric
    totals = grouped_values(dimension_index, metric_index)
    if len(totals) < 2:
        return None
    dimension_is_date = dimension_index in date_indexes
    items = sorted(totals.items()) if dimension_is_date else sorted(totals.items(), key=lambda item: item[1], reverse=True)

    if requested_type == "heatmap":
        return None
    if requested_type in {"scatter", "histogram", "box"}:
        return None
    if requested_type in {"line", "area"} and not dimension_is_date:
        return None
    if requested_type == "pie" and (any(value < 0 for _, value in items) or sum(value for _, value in items) <= 0):
        return None

    metric_key = re.sub(r"[^a-z0-9]+", "_", str(columns[metric_index]).lower()).strip("_")
    composition = bool(re.search(r"(^|_)(count|headcount|employees|employee_count|share|percent|percentage|proportion)(_|$)", metric_key))
    if requested_type:
        chart_type = requested_type
    elif dimension_is_date:
        chart_type = "line"
    elif len(items) <= 6 and composition and all(value >= 0 for _, value in items) and sum(value for _, value in items) > 0:
        chart_type = "donut"
    else:
        chart_type = "column" if len(items) <= 6 else "bar"
    x_label = name(metric_index) if chart_type == "bar" else name(dimension_index)
    y_label = name(dimension_index) if chart_type == "bar" else name(metric_index)
    return base(chart_type, f"{name(metric_index)} {'over' if dimension_is_date else 'by'} {name(dimension_index)}",
                [item[0] for item in items], [round(item[1], 4) for item in items], metric_index, dimension_index,
                x_label=x_label, y_label=y_label)


def _chart_unit(metric):
    lowered = metric.lower()
    if "salary" in lowered or "amount" in lowered or "revenue" in lowered or "cost" in lowered:
        return "units as reported"
    if "percent" in lowered or "rate" in lowered or "share" in lowered:
        return "%"
    if "rating" in lowered:
        return "rating points"
    if "count" in lowered or "employees" in lowered or "headcount" in lowered:
        return "employees"
    return "records"


def _percentile(values, fraction):
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] * (upper - position) + values[upper] * (position - lower)


def requested_chart_type(question):
    text = str(question or "").lower()
    requests = (
        ("scatter", r"\bscatter(?:\s*plot)?\b"),
        ("histogram", r"\bhistograms?\b|\bdistribution\s+(?:chart|plot)\b"),
        ("box", r"\bbox\s*[- ]?plots?\b|\bboxplots?\b"),
        ("heatmap", r"\bheat\s*maps?\b|\bheatmaps?\b"),
        ("area", r"\barea\s+charts?\b|\barea\s+plots?\b"),
        ("pie", r"\bpie(?:\s+charts?)?\b|\bdoughnuts?\b|\bdonut\s+charts?\b"),
        ("line", r"\bline(?:\s+charts?)?\b|\btrend\s+charts?\b"),
        ("column", r"\bcolumn(?:\s+charts?)?\b|\bvertical\s+bars?\b"),
        ("bar", r"\bbar(?:\s+charts?)?\b|\bhorizontal\s+bars?\b"),
    )
    for chart_type, pattern in requests:
        if re.search(pattern, text):
            return chart_type
    return None


def chart_unavailable_message(chart_type, result, alternative=None):
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    numeric_count = sum(
        1 for index, column in enumerate(columns)
        if not is_sensitive_column(column) and not ID_COLUMN.search(str(column).lower())
        and any(index < len(row) and isinstance(row[index], (int, float)) for row in rows)
    )
    category_count = sum(
        1 for index, column in enumerate(columns)
        if not is_sensitive_column(column) and not ID_COLUMN.search(str(column).lower())
        and any(index < len(row) and isinstance(row[index], str) for row in rows)
    )
    requirements = {
        "scatter": "a scatter plot needs two shareable numeric measures and at least three paired observations",
        "histogram": "a histogram needs at least one numeric observation in one measure",
        "box": "a box plot needs at least one numeric observation in one measure",
        "heatmap": "a heatmap needs either paired numeric measures for correlation or two categories and a numeric measure",
        "line": "a line chart needs an ordered time or date dimension and a numeric measure",
        "area": "an area chart needs an ordered time or date dimension and a numeric measure",
        "pie": "a pie chart needs a small set of non-negative category values",
        "column": "a column chart needs a category and a numeric measure",
        "bar": "a bar chart needs a category and a numeric measure",
    }
    explanation = requirements.get(chart_type, "the requested chart needs more suitable result data")
    if chart_type == "scatter" and numeric_count < 2:
        explanation += f"; these results contain only {numeric_count} shareable numeric measure(s)"
    elif chart_type == "heatmap" and category_count < 2:
        explanation += f"; these results contain only {category_count} shareable category dimension(s)"
    elif not rows and chart_type in {"histogram", "box"}:
        explanation += "; no result rows were returned"
    if alternative:
        return f"I couldn’t create the requested {chart_type} chart because {explanation}. I’ve shown a {alternative} chart instead."
    return f"I couldn’t create the requested {chart_type} chart because {explanation}. Try asking for a chart that matches the available measures and categories."


def create_business_report_pdf(report):
    output = io.BytesIO()
    green = colors.HexColor("#176344")
    deep_green = colors.HexColor("#123e2c")
    ink = colors.HexColor("#24372d")
    muted = colors.HexColor("#69786f")
    line = colors.HexColor("#e0e8e2")
    pale = colors.HexColor("#f3f7f4")
    white = colors.white
    margin = 0.72 * inch
    document = SimpleDocTemplate(
        output, pagesize=letter, leftMargin=margin, rightMargin=margin,
        topMargin=0.78 * inch, bottomMargin=0.7 * inch,
        title="Employee Insights Business Report", author="Employee Insights Assistant",
    )
    content_width = letter[0] - margin * 2
    fonts = _report_fonts()
    styles = {
        "eyebrow": ParagraphStyle("ReportEyebrow", fontName=fonts["bold"], fontSize=8, leading=11, textColor=green, spaceAfter=5),
        "title": ParagraphStyle("ReportTitle", fontName=fonts["bold"], fontSize=25, leading=29, textColor=deep_green, spaceAfter=5),
        "subtitle": ParagraphStyle("ReportSubtitle", fontName=fonts["regular"], fontSize=9, leading=13, textColor=muted),
        "section": ParagraphStyle("ReportSection", fontName=fonts["bold"], fontSize=12, leading=15, textColor=ink, spaceBefore=8, spaceAfter=7, keepWithNext=True),
        "body": ParagraphStyle("ReportBody", fontName=fonts["regular"], fontSize=9.5, leading=14, textColor=ink),
        "question": ParagraphStyle("ReportQuestion", fontName=fonts["bold"], fontSize=10, leading=15, textColor=ink),
        "summary_label": ParagraphStyle("ReportSummaryLabel", fontName=fonts["bold"], fontSize=8, leading=11, textColor=colors.HexColor("#a9d8bd"), spaceAfter=5),
        "summary": ParagraphStyle("ReportSummary", fontName=fonts["regular"], fontSize=10, leading=15, textColor=white),
        "table_header": ParagraphStyle("ReportTableHeader", fontName=fonts["bold"], fontSize=8, leading=10, textColor=white),
        "chart_title": ParagraphStyle("ReportChartTitle", fontName=fonts["bold"], fontSize=9, leading=12, textColor=ink),
        "chart_label": ParagraphStyle("ReportChartLabel", fontName=fonts["regular"], fontSize=8, leading=10, textColor=muted),
        "chart_value": ParagraphStyle("ReportChartValue", fontName=fonts["bold"], fontSize=8, leading=10, textColor=ink, alignment=TA_RIGHT),
        "note": ParagraphStyle("ReportNote", fontName=fonts["regular"], fontSize=8, leading=11, textColor=muted),
        "bullet": ParagraphStyle("ReportBullet", fontName=fonts["regular"], fontSize=9, leading=13, textColor=ink, leftIndent=12, firstLineIndent=-9, spaceAfter=4),
        "action_number": ParagraphStyle("ReportActionNumber", fontName=fonts["bold"], fontSize=10, leading=14, textColor=green),
        "chart_interpretation": ParagraphStyle("ReportChartInterpretation", fontName=fonts["regular"], fontSize=9, leading=13, textColor=ink, spaceBefore=8),
    }

    def paragraph(value, style):
        return Paragraph(escape(str(value or "")).replace("\n", "<br/>"), style)

    def section(title, content):
        items = content if isinstance(content, list) else [content]
        return KeepTogether([paragraph(title.upper(), styles["section"]), *items])

    def report_table_cell(value, header):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and "salary" in str(header).casefold():
            value = f"{value:,.2f}"
        return paragraph(value, styles["note"])

    class ResultChart(Flowable):
        def __init__(self, chart_data):
            super().__init__()
            self.chart_data = chart_data
            self.labels = [str(value) for value in chart_data.get("labels", [])]
            self.values = [float(value) for value in chart_data.get("values", [])]
            self.chart_type = chart_data.get("type", "bar")
            self.groups = chart_data.get("groups", [])
            bar_left = min(145, (letter[0] - margin * 2) * 0.33)
            label_style = ParagraphStyle("ChartCategoryLabel", fontName=fonts["regular"], fontSize=6.5, leading=7.5)
            label_heights = []
            for label in self.labels:
                label_paragraph = Paragraph(escape(label), label_style)
                _, label_height = label_paragraph.wrap(max(20, bar_left - 8), 1000)
                label_heights.append(label_height)
            bar_height = max(24, max(label_heights, default=0) + 6) * len(self.labels) + 12
            if self.chart_type == "box":
                self.height = max(205, 26 * len(self.groups) + 40)
            elif self.chart_type == "bar":
                self.height = max(205, bar_height)
            elif self.chart_type in {"line", "area", "column", "histogram"}:
                self.height = 260
            else:
                self.height = 190 if self.chart_type in {"pie", "donut"} else 230 if self.chart_type == "heatmap" else 205

        def wrap(self, available_width, available_height):
            self.width = available_width
            return self.width, self.height

        def draw(self):
            canvas = self.canv
            plot_left = 76
            plot_right = self.width - 12
            plot_bottom = 75 if self.chart_type in {"line", "area", "column", "histogram"} else 28
            plot_top = self.height - 17
            count = len(self.values)
            maximum = max([0.0] + self.values) or 1.0
            minimum = min([0.0] + self.values)
            palette = [colors.HexColor(value) for value in CHART_PALETTE]
            canvas.setFont(fonts["regular"], 7)
            canvas.setFillColor(muted)

            if self.chart_type in {"pie", "donut"}:
                if not count:
                    return
                total = sum(max(0, value) for value in self.values)
                if total <= 0:
                    return
                center_x = min(110, self.width * 0.28)
                center_y = self.height / 2
                outer_radius = min(61, self.height * 0.35)
                inner_radius = outer_radius * 0.57 if self.chart_type == "donut" else 0
                angle = 0.0
                for index, value in enumerate(self.values):
                    sweep = max(0, value) / total * math.tau
                    if sweep <= 0:
                        continue
                    steps = max(2, int(sweep * 24))
                    points = [(center_x, center_y)] if not inner_radius else []
                    for step in range(steps + 1):
                        theta = angle + sweep * step / steps - math.pi / 2
                        points.append((center_x + outer_radius * math.cos(theta), center_y + outer_radius * math.sin(theta)))
                    if inner_radius:
                        for step in range(steps, -1, -1):
                            theta = angle + sweep * step / steps - math.pi / 2
                            points.append((center_x + inner_radius * math.cos(theta), center_y + inner_radius * math.sin(theta)))
                    path = canvas.beginPath()
                    path.moveTo(*points[0])
                    for point in points[1:]:
                        path.lineTo(*point)
                    path.close()
                    canvas.setFillColor(palette[index % len(palette)])
                    canvas.drawPath(path, fill=1, stroke=0)
                    angle += sweep
                legend_x = center_x + outer_radius + 20
                legend_y = self.height - 30
                canvas.setFont(fonts["regular"], 8)
                for index, (label, value) in enumerate(zip(self.labels, self.values)):
                    y = legend_y - index * 21
                    if y < 12:
                        break
                    canvas.setFillColor(palette[index % len(palette)])
                    canvas.circle(legend_x, y + 3, 4, fill=1, stroke=0)
                    canvas.setFillColor(ink)
                    canvas.drawString(legend_x + 10, y, label)
                    canvas.setFillColor(muted)
                    share = value / total * 100
                    value_format = f"{value:,.2f}" if "salary" in str(self.chart_data.get("metric", "")).casefold() else f"{value:,.2f}".rstrip("0").rstrip(".")
                    canvas.drawRightString(self.width - 8, y, value_format + f"  ({share:.0f}%)")
                return

            if self.chart_type == "scatter":
                points = self.chart_data.get("points", [])
                points = [point for point in points if isinstance(point, dict) and isinstance(point.get("x"), (int, float)) and isinstance(point.get("y"), (int, float))][:300]
                if len(points) < 3:
                    return
                x_values = [float(point["x"]) for point in points]
                y_values = [float(point["y"]) for point in points]
                x_min, x_max = min(x_values), max(x_values)
                y_min, y_max = min(y_values), max(y_values)
                x_range, y_range = max(x_max - x_min, 1), max(y_max - y_min, 1)
                left, right, bottom, top = 48, self.width - 8, 30, self.height - 16
                canvas.setStrokeColor(line)
                for tick in range(5):
                    x = left + (right - left) * tick / 4
                    y = bottom + (top - bottom) * tick / 4
                    canvas.line(x, bottom, x, top)
                    canvas.line(left, y, right, y)
                    canvas.setFillColor(muted)
                    x_value = x_min + (x_max - x_min) * tick / 4
                    x_format = f"{x_value:,.2f}" if "salary" in str(self.chart_data.get("x_label", "")).casefold() else f"{x_value:,.3g}"
                    canvas.drawCentredString(x, bottom - 12, x_format)
                    canvas.drawRightString(left - 4, y - 2, f"{y_min + (y_max - y_min) * tick / 4:,.3g}")
                canvas.setFillColor(green)
                for x_value, y_value in zip(x_values, y_values):
                    x = left + (x_value - x_min) / x_range * (right - left)
                    y = bottom + (y_value - y_min) / y_range * (top - bottom)
                    canvas.circle(x, y, 2.4, fill=1, stroke=0)
                canvas.setFillColor(muted)
                canvas.drawCentredString((left + right) / 2, 7, self.chart_data.get("x_label", "X")[:45])
                canvas.drawString(0, self.height - 8, self.chart_data.get("y_label", "Y")[:45])
                return

            if self.chart_type == "box":
                groups = self.groups
                values = [
                    float(value)
                    for group in groups
                    for value in (group.get("min"), group.get("max"), *group.get("outliers", []))
                    if isinstance(value, (int, float))
                ]
                if not groups or not values:
                    return
                data_min, data_max = min(values), max(values)
                value_range = max(data_max - data_min, 1)
                left, right = min(130, self.width * 0.3), self.width - 10
                row_height = min(22, (self.height - 25) / len(groups))
                for index, group in enumerate(groups):
                    y = self.height - 16 - (index + 0.5) * row_height
                    x = lambda value: left + (float(value) - data_min) / value_range * (right - left)
                    canvas.setFillColor(muted)
                    label = str(group.get("label", "Group"))
                    canvas.setFont(fonts["regular"], 6)
                    canvas.drawRightString(left - 6, y - 2, label)
                    canvas.setStrokeColor(green)
                    canvas.setLineWidth(1)
                    canvas.line(x(group["min"]), y, x(group["max"]), y)
                    canvas.setFillColor(colors.HexColor("#b0c9b7"))
                    canvas.rect(x(group["q1"]), y - 5, max(1, x(group["q3"]) - x(group["q1"])), 10, fill=1, stroke=1)
                    canvas.setStrokeColor(deep_green)
                    canvas.line(x(group["median"]), y - 6, x(group["median"]), y + 6)
                canvas.setFillColor(muted)
                for tick in range(5):
                    tick_value = data_min + value_range * tick / 4
                    tick_format = f"{tick_value:,.2f}" if "salary" in str(self.chart_data.get("x_label", "")).casefold() else f"{tick_value:,.3g}"
                    canvas.drawCentredString(left + (right - left) * tick / 4, 12, tick_format)
                canvas.drawCentredString((left + right) / 2, 5, self.chart_data.get("x_label", "Measure")[:45])
                return

            if self.chart_type == "heatmap":
                x_labels = self.chart_data.get("x_labels", [])
                y_labels = self.chart_data.get("y_labels", [])
                matrix = self.chart_data.get("matrix", [])
                values = [float(value) for row in matrix for value in row if isinstance(value, (int, float))]
                if len(x_labels) < 2 or len(y_labels) < 2 or not values:
                    return
                data_min, data_max = min(values), max(values)
                value_range = max(data_max - data_min, 1)
                left = min(105, self.width * 0.23)
                top = self.height - 26
                cell_width = (self.width - left - 8) / len(x_labels)
                cell_height = (self.height - 65) / len(y_labels)
                for row_index, label in enumerate(y_labels):
                    y = top - (row_index + 1) * cell_height
                    canvas.setFillColor(muted)
                    canvas.setFont(fonts["regular"], 5.5)
                    canvas.drawRightString(left - 5, y + cell_height / 2 - 2, str(label))
                    for column_index, x_label in enumerate(x_labels):
                        value = float(matrix[row_index][column_index])
                        intensity = max(0, min(1, (value - data_min) / value_range))
                        red = 0.92 - 0.48 * intensity
                        green_channel = 0.96 - 0.35 * intensity
                        blue = 0.93 - 0.45 * intensity
                        canvas.setFillColor(colors.Color(red, green_channel, blue))
                        canvas.rect(left + column_index * cell_width, y, cell_width - 1, cell_height - 1, fill=1, stroke=0)
                        if len(x_labels) <= 7 and len(y_labels) <= 7:
                            canvas.setFillColor(ink)
                            canvas.drawCentredString(left + (column_index + 0.5) * cell_width, y + cell_height / 2 - 2, f"{value:,.1f}")
                canvas.setFillColor(muted)
                for index, label in enumerate(x_labels):
                    canvas.saveState()
                    canvas.translate(left + (index + 0.5) * cell_width, 3)
                    canvas.rotate(45)
                    canvas.setFont(fonts["regular"], 5)
                    canvas.drawString(0, 0, str(label))
                    canvas.restoreState()
                legend_left = left + 45
                legend_width = min(120, self.width - legend_left - 8)
                for index in range(8):
                    intensity = index / 7
                    color = colors.Color(0.92 - 0.48 * intensity, 0.96 - 0.35 * intensity, 0.93 - 0.45 * intensity)
                    canvas.setFillColor(color)
                    canvas.rect(legend_left + legend_width * index / 8, 17, legend_width / 8, 7, fill=1, stroke=0)
                canvas.setFillColor(muted)
                canvas.drawString(legend_left, 8, f"{data_min:,.3g}")
                canvas.drawRightString(legend_left + legend_width, 8, f"{data_max:,.3g}")
                return

            if self.chart_type == "bar":
                if not count:
                    return
                row_height = self.height / max(count, 1)
                label_x = 0
                bar_left = min(145, self.width * 0.33)
                bar_width = self.width - bar_left - 57
                for index, (label, value) in enumerate(zip(self.labels, self.values)):
                    y = self.height - (index + 1) * row_height + row_height / 2
                    canvas.setFillColor(muted)
                    canvas.setFont(fonts["regular"], 7)
                    canvas.setFont(fonts["regular"], 6.5)
                    label_paragraph = Paragraph(escape(label), ParagraphStyle(
                        "BarCategoryLabel", fontName=fonts["regular"], fontSize=6.5,
                        leading=7.5, textColor=muted, alignment=TA_RIGHT,
                    ))
                    _, label_height = label_paragraph.wrap(max(20, bar_left - 8), row_height - 2)
                    label_paragraph.drawOn(canvas, 0, y - label_height / 2)
                    canvas.setFillColor(colors.HexColor("#e8efe9"))
                    canvas.roundRect(bar_left, y - 3, bar_width, 7, 3.5, fill=1, stroke=0)
                    canvas.setFillColor(green)
                    canvas.roundRect(bar_left, y - 3, max(2, bar_width * max(0, value) / maximum), 7, 3.5, fill=1, stroke=0)
                    canvas.setFillColor(ink)
                    value_format = f"{value:,.2f}" if "salary" in str(self.chart_data.get("metric", "")).casefold() else f"{value:,.2f}".rstrip("0").rstrip(".")
                    canvas.drawRightString(self.width, y - 2, value_format)
                return

            plot_width = plot_right - plot_left
            plot_height = plot_top - plot_bottom
            value_range = max(maximum - minimum, 1)
            for tick in range(5):
                y = plot_bottom + plot_height * tick / 4
                canvas.setStrokeColor(line)
                canvas.setLineWidth(0.5)
                canvas.line(plot_left, y, plot_right, y)
            points = []
            for index, value in enumerate(self.values):
                x = plot_left + (plot_width / 2 if count == 1 else plot_width * index / (count - 1))
                y = plot_bottom + (value - minimum) / value_range * plot_height
                points.append((x, y))
            if self.chart_type in {"line", "area"}:
                path = canvas.beginPath()
                path.moveTo(*points[0])
                for point in points[1:]:
                    path.lineTo(*point)
                if self.chart_type == "area":
                    zero_y = plot_bottom + (0 - minimum) / value_range * plot_height
                    area_path = canvas.beginPath()
                    area_path.moveTo(points[0][0], zero_y)
                    area_path.lineTo(*points[0])
                    for point in points[1:]:
                        area_path.lineTo(*point)
                    area_path.lineTo(points[-1][0], zero_y)
                    area_path.close()
                    canvas.setFillColor(colors.HexColor("#d5e1d8"))
                    canvas.drawPath(area_path, stroke=0, fill=1)
                canvas.setStrokeColor(green)
                canvas.setLineWidth(2)
                canvas.drawPath(path, stroke=1, fill=0)
                for index, (x, y) in enumerate(points):
                    canvas.setFillColor(palette[index % len(palette)])
                    canvas.circle(x, y, 3.5, fill=1, stroke=0)
                    canvas.setFillColor(ink)
                    canvas.setFont(fonts["regular"], 7)
                    value_format = f"{self.values[index]:,.2f}" if "salary" in str(self.chart_data.get("metric", "")).casefold() else f"{self.values[index]:,.2f}".rstrip("0").rstrip(".")
                    canvas.drawCentredString(x, y + 7, value_format)
            else:
                slot_width = plot_width / count
                bar_width = slot_width * 0.58
                zero_y = plot_bottom + (0 - minimum) / value_range * plot_height
                for index, (x, value) in enumerate(points):
                    height = max(1, abs(value - zero_y))
                    canvas.setFillColor(palette[index % len(palette)])
                    canvas.roundRect(x - bar_width / 2, min(value, zero_y), bar_width, height, 3, fill=1, stroke=0)
                    canvas.setFillColor(ink)
                    canvas.setFont(fonts["regular"], 7)
                    value_format = f"{self.values[index]:,.2f}" if "salary" in str(self.chart_data.get("metric", "")).casefold() else f"{self.values[index]:,.2f}".rstrip("0").rstrip(".")
                    canvas.drawCentredString(x, max(value, zero_y) + 4, value_format)
            canvas.setFillColor(muted)
            canvas.setFont(fonts["regular"], 7)
            for index, (x, _) in enumerate(points):
                canvas.saveState()
                canvas.translate(x, 25)
                canvas.rotate(45)
                canvas.drawString(0, 0, self.labels[index])
                canvas.restoreState()

    def draw_page(canvas, doc):
        canvas.saveState()
        page_width, page_height = letter
        canvas.setFillColor(green)
        canvas.roundRect(margin, page_height - 39, 19, 19, 5, fill=1, stroke=0)
        canvas.setFillColor(white)
        canvas.setFont(fonts["bold"], 10)
        canvas.drawCentredString(margin + 9.5, page_height - 33, "E")
        canvas.setFillColor(ink)
        canvas.setFont(fonts["bold"], 8)
        canvas.drawString(margin + 27, page_height - 31, "EMPLOYEE INSIGHTS")
        canvas.setFillColor(muted)
        canvas.setFont(fonts["regular"], 7)
        canvas.drawRightString(page_width - margin, page_height - 31, "BUSINESS REPORT")
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.7)
        canvas.line(margin, page_height - 48, page_width - margin, page_height - 48)
        canvas.line(margin, 39, page_width - margin, 39)
        canvas.setFillColor(muted)
        canvas.setFont(fonts["regular"], 7)
        canvas.drawString(margin, 26, "© 2026. All rights reserved.")
        canvas.drawCentredString(page_width / 2, 26, f"{doc.page}")
        canvas.drawRightString(page_width - margin, 26, "Created by Mary Prathyusha Basani")
        canvas.restoreState()

    story = [
        paragraph("WORKFORCE ANALYSIS  /  BUSINESS REPORT", styles["eyebrow"]),
        paragraph(report.get("title", "Employee Workforce Business Report"), styles["title"]),
        paragraph(f"Prepared {datetime.now().strftime('%B %d, %Y')}  ·  Workforce insights", styles["subtitle"]),
        Spacer(1, 16),
    ]
    question = Table([
        [paragraph("BUSINESS QUESTION", styles["eyebrow"])],
        [paragraph(report.get("question", ""), styles["question"])],
    ], colWidths=[content_width])
    question.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), 0.6, line),
        ("LINEBEFORE", (0, 0), (0, -1), 3, green), ("LEFTPADDING", (0, 0), (-1, -1), 13),
        ("RIGHTPADDING", (0, 0), (-1, -1), 13), ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2), ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 11),
    ]))
    story.extend([question, Spacer(1, 14)])
    summary = Table([
        [paragraph("EXECUTIVE SUMMARY", styles["summary_label"])],
        [paragraph(report.get("answer", "No summary was returned for this analysis."), styles["summary"])],
    ], colWidths=[content_width])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), deep_green), ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14), ("TOPPADDING", (0, 0), (-1, 0), 12),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1), ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 13),
    ]))
    story.extend([summary, Spacer(1, 10)])

    objective_scope = Table([
        [paragraph("OBJECTIVE", styles["eyebrow"]), paragraph("SCOPE", styles["eyebrow"])],
        [paragraph(report.get("business_objective", report.get("question", "")), styles["body"]),
         paragraph(report.get("scope", "Analysis covers the result set returned for this question."), styles["body"])],
    ], colWidths=[content_width * 0.5, content_width * 0.5])
    objective_scope.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), 0.5, line),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([section("Business objective and scope", objective_scope), Spacer(1, 4)])

    methodology = [
        paragraph(f"Data source: {report.get('data_source', 'Employee workforce records relevant to the requested analysis.')}", styles["body"]),
        paragraph(f"Cleaning and methodology: {report.get('methodology', 'A read-only query was analyzed using a normalized in-memory copy. The source database was not changed.')}", styles["body"]),
    ]
    story.extend([section("Data sources, cleaning, and methodology", methodology), Spacer(1, 4)])

    report_charts = report.get("charts") or []
    chart = report.get("chart") or {}
    if report_charts:
        pass
    elif chart.get("type"):
        x_label = chart.get("x_label")
        y_label = chart.get("y_label")
        x_unit = chart.get("x_unit")
        y_unit = chart.get("y_unit")
        chart_context = " · ".join(value for value in [
            f"X: {x_label}{f' ({x_unit})' if x_unit else ''}" if x_label else None,
            f"Y: {y_label}{f' ({y_unit})' if y_unit else ''}" if y_label else None,
            f"Units: {chart.get('unit')}" if chart.get("unit") and not y_unit else None,
        ] if value)
        chart_group = [paragraph(chart.get("title", "Results"), styles["chart_title"])]
        if chart_context:
            chart_group.append(paragraph(chart_context, styles["chart_label"]))
        chart_group.extend([Spacer(1, 5), ResultChart(chart)])
        story.extend([section("Key findings, charts, and supporting figures", chart_group), Spacer(1, 4)])
    elif report.get("chart_notice"):
        story.extend([section(
            "Chart availability",
            paragraph(report["chart_notice"], styles["note"]),
        ), Spacer(1, 4)])

    for table_data in report.get("tables", []):
        headers = table_data.get("columns", [])
        rows = table_data.get("rows", [])
        if not headers or not rows:
            continue
        table_cells = [[paragraph(value, styles["table_header"]) for value in headers]]
        table_cells.extend([
            [report_table_cell(value, headers[index]) for index, value in enumerate(row)]
            for row in rows
        ])
        column_width = content_width / len(headers)
        result_table = Table(
            table_cells,
            colWidths=[column_width] * len(headers),
            repeatRows=1,
            hAlign="LEFT",
        )
        result_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), deep_green),
            ("TEXTCOLOR", (0, 0), (-1, 0), white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, pale]),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([
            paragraph(table_data.get("title", "Supporting data"), styles["chart_title"]),
            result_table,
            Spacer(1, 8),
        ])

    insights = report.get("insights") or []
    if insights:
        finding_list = [paragraph(f"•  {item}", styles["bullet"]) for item in insights[:3]]
        story.extend([section("Key findings: verified", finding_list), Spacer(1, 2)])

    recommendations = report.get("recommendations") or []
    possible_explanations = report.get("possible_explanations") or []
    if possible_explanations:
        explanation_list = [paragraph(f"•  {item if str(item).lower().startswith('possible explanation:') else f'Possible explanation: {item}' }", styles["bullet"]) for item in possible_explanations[:2]]
        story.extend([section("Possible explanations: not verified", explanation_list), Spacer(1, 2)])

    chart_notice = report.get("chart_notice")
    if chart_notice:
        story.extend([section("Chart availability", paragraph(chart_notice, styles["note"])), Spacer(1, 2)])

    implications = report.get("implications") or []
    if implications:
        implication_list = [paragraph(f"•  {item}", styles["bullet"]) for item in implications[:2]]
        story.extend([section("Business implications", implication_list), Spacer(1, 2)])
    if recommendations:
        actions = []
        for index, recommendation in enumerate(recommendations[:3], start=1):
            actions.append([
                paragraph(f"{index:02d}", styles["action_number"]),
                paragraph(recommendation, styles["body"]),
            ])
        action_table = Table(actions, colWidths=[0.45 * inch, content_width - 0.45 * inch])
        action_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), 0.5, line),
            ("LINEBEFORE", (0, 0), (0, -1), 2, green), ("LINEBELOW", (0, 0), (-1, -2), 0.5, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.extend([section("Recommended next steps", action_table), Spacer(1, 4)])

    limitations = report.get("limitations") or []
    limitation_items = [str(item) for item in limitations if isinstance(item, str) and item.strip()]
    if report.get("truncated"):
        limitation_items.append("The returned result was limited and may not represent the full workforce.")
    if not limitation_items:
        limitation_items.append("Findings describe only the fields and records returned for this question; causal explanations and unqueried business targets were not independently validated.")
    conclusion = report.get("conclusion") or report.get("answer", "No conclusion was returned for this analysis.")
    limitations_and_conclusion = [paragraph(f"Limitations: {' '.join(limitation_items[:3])}", styles["body"])]
    if conclusion and conclusion != report.get("answer"):
        limitations_and_conclusion.append(paragraph(f"Conclusion: {conclusion}", styles["body"]))
    story.extend([section("Data limitations and conclusion", limitations_and_conclusion)])

    if report_charts:
        for chart_item in report_charts:
            chart_data = chart_item.get("chart", {})
            if not chart_data.get("type"):
                continue
            story.extend([
                PageBreak(),
                paragraph(chart_data.get("title", "Workforce chart"), styles["title"]),
            ])
            chart_context = " · ".join(value for value in [
                f"X: {chart_data['x_label']}{f' ({chart_data.get("x_unit")})' if chart_data.get('x_unit') else ''}"
                if chart_data.get("x_label") else None,
                f"Y: {chart_data['y_label']}{f' ({chart_data.get("y_unit")})' if chart_data.get('y_unit') else ''}"
                if chart_data.get("y_label") else None,
                f"Units: {chart_data.get('unit')}" if chart_data.get("unit") else None,
            ] if value)
            if chart_context:
                story.append(paragraph(chart_context, styles["chart_label"]))
            story.extend([
                Spacer(1, 12),
                ResultChart(chart_data),
                paragraph(chart_item.get("interpretation", "Interpretation is limited to the values shown in this chart."), styles["chart_interpretation"]),
            ])

        dashboard = report.get("dashboard") or {}
        kpis = dashboard.get("kpis", [])[:6]
        dashboard_findings = [paragraph(f"•  {item}", styles["bullet"]) for item in dashboard.get("findings", [])[:4]]
        dashboard_actions = [paragraph(f"{index:02d}  {item}", styles["body"])
                             for index, item in enumerate(dashboard.get("actions", [])[:4], start=1)]
        if kpis or dashboard_findings or dashboard_actions:
            story.extend([PageBreak(), paragraph("Workforce dashboard", styles["title"])])
        if kpis:
            kpi_cells = [[
                Paragraph(
                    f"{escape(str(item.get('label', 'KPI')))}<br/><b>{escape(str(item.get('value', '—')))}</b>",
                    styles["body"],
                )
                for item in kpis[index:index + 3]
            ] for index in range(0, len(kpis), 3)]
            kpi_table = Table(kpi_cells, colWidths=[content_width / 3] * 3)
            kpi_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), 0.5, line),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            story.extend([kpi_table, Spacer(1, 14)])
        if dashboard_findings:
            story.extend([section("Main findings", dashboard_findings), Spacer(1, 8)])
        if dashboard_actions:
            story.extend([section("Recommended actions", dashboard_actions)])

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return output.getvalue()


def send_report_email(pdf_bytes, recipient, question, subject=None, body=None, filename=None):
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM_EMAIL", "").strip()
    if not all((host, username, password, sender)):
        return "not_configured"
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes"}
        use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
        message = EmailMessage()
        safe_subject = " ".join(str(subject or "Your Employee Insights business report").split())[:180]
        safe_filename = os.path.basename(str(filename or "employee-insights-business-report.pdf"))
        safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", safe_filename).strip(".-_") or "business-report.pdf"
        safe_filename = safe_filename[:120]
        message["Subject"] = safe_subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(
            str(body).strip()[:10_000]
            if isinstance(body, str) and body.strip()
            else "Your requested workforce analysis report is attached. Findings are based on the returned query results."
        )
        message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=safe_filename)
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as server:
                server.login(username, password)
                refused = server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                server.login(username, password)
                refused = server.send_message(message)
        return "failed" if refused else "sent"
    except Exception as error:
        smtp_code = getattr(error, "smtp_code", None)
        response = f", SMTP response {smtp_code}" if isinstance(smtp_code, int) else ""
        logging.getLogger(__name__).error(
            "Report email delivery failed (%s%s)", type(error).__name__, response
        )
        return "failed"


def valid_email_address(value):
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_PATTERN.fullmatch(value.strip()))
