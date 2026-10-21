import json
import hmac
import os
import re
import sys
import threading
import uuid
import math
import textwrap
from collections import Counter
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import has_request_context, jsonify, request, send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parent
CHATBOT_ROOT = PROJECT_ROOT

for import_path in (PROJECT_ROOT, CHATBOT_ROOT):
    import_path_text = str(import_path)
    if import_path_text not in sys.path:
        sys.path.insert(0, import_path_text)

load_dotenv(PROJECT_ROOT / ".env")

import business_reports as report_helpers
import chatbot_app as chatbot

app = chatbot.app
SQL_LOG_PATH = Path(__file__).resolve().parent / "sql_queries.log"
_sql_log_lock = threading.Lock()
SQL_LOG_PATH.touch(exist_ok=True)


@app.get("/healthz")
def health_check():
    return jsonify(status="ok")


@app.before_request
def require_app_access():
    if request.path == "/healthz":
        return None
    expected_username = os.getenv("APP_ACCESS_USERNAME", "")
    expected_password = os.getenv("APP_ACCESS_PASSWORD", "")
    if not expected_username or not expected_password:
        return jsonify(error="Application access credentials are not configured."), 503
    credentials = request.authorization
    if (
        credentials is None
        or not hmac.compare_digest(credentials.username or "", expected_username)
        or not hmac.compare_digest(credentials.password or "", expected_password)
    ):
        response = jsonify(error="Authentication required.")
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="Employee Insights", charset="UTF-8"'
        return response
    return None


def _write_sql_log(prompt, sql, parameters=None):
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    safe_prompt = " ".join(str(prompt or "").split()) or "Prompt unavailable"
    safe_parameter_keys = {"status", "department", "year", "min_year", "max_year", "salary", "min_salary", "max_salary", "limit", "top_n"}
    safe_parameters = {
        str(key): value if str(key).casefold() in safe_parameter_keys else "[redacted]"
        for key, value in (parameters or {}).items()
    }
    with _sql_log_lock:
        with SQL_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{timestamp}] Prompt: {safe_prompt}\n"
                f"SQL:\n{str(sql).strip()}\n"
                f"Parameters: {json.dumps(safe_parameters, ensure_ascii=True, default=str)}\n\n"
            )


_original_execute_query = chatbot.execute_query


def _logged_execute_query(sql, *args, **kwargs):
    prompt = ""
    if has_request_context():
        body = request.get_json(silent=True) or {}
        prompt = body.get("requirement", "") if isinstance(body, dict) else ""
    try:
        _write_sql_log(prompt, sql, kwargs.get("parameters"))
    except OSError:
        app.logger.exception("Could not append the MARY SQL query log")
    return _original_execute_query(sql, *args, **kwargs)


chatbot.execute_query = _logged_execute_query


def _build_analysis_response(prompt, result, explanation, analysis, actions):
    result_id = uuid.uuid4().hex
    context_id = chatbot.store_query_context(
        prompt,
        result,
        explanation,
        actions,
        result_id=result_id,
        analysis=analysis,
    )
    return jsonify(
        explanation=explanation,
        analysis=analysis,
        requested_actions=actions,
        result_id=result_id,
        context_id=context_id,
    )


def _create_monthly_joiner_pdf(year, chart, explanation, limitation):
    output = BytesIO()
    page_width, page_height = letter
    pdf = pdf_canvas.Canvas(output, pagesize=letter)
    pdf.setTitle(f"Monthly Joiners in {year}")
    pdf.setAuthor("MARY")

    margin = 48
    green = colors.HexColor("#176344")
    ink = colors.HexColor("#24372d")
    muted = colors.HexColor("#69786f")
    grid = colors.HexColor("#e0e8e2")
    pdf.setFillColor(green)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(margin, page_height - 42, "MARY  |  EMPLOYEE INSIGHTS")
    pdf.setStrokeColor(grid)
    pdf.line(margin, page_height - 54, page_width - margin, page_height - 54)

    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 21)
    pdf.drawString(margin, page_height - 88, f"Monthly joiners in {year}")
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(muted)
    summary_lines = textwrap.wrap(explanation, width=105)
    summary_y = page_height - 108
    for line in summary_lines[:2]:
        pdf.drawString(margin, summary_y, line)
        summary_y -= 13

    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin, page_height - 155, f"Monthly counts for {year}")

    values = [float(value) for value in chart.get("values", [])]
    labels = [str(value) for value in chart.get("labels", [])]
    if not values or len(values) != len(labels):
        raise ValueError("Monthly joining chart requires matching month labels and values.")

    plot_left = margin + 38
    plot_right = page_width - margin - 8
    plot_bottom = 190
    plot_top = page_height - 188
    plot_height = plot_top - plot_bottom
    max_value = max(values, default=0)
    tick_step = max(1, math.ceil(max_value / 4))
    upper_bound = tick_step * 4

    pdf.setFont("Helvetica", 8)
    for tick in range(5):
        tick_value = tick_step * tick
        y_position = plot_bottom + plot_height * tick / 4
        pdf.setStrokeColor(grid)
        pdf.line(plot_left, y_position, plot_right, y_position)
        pdf.setFillColor(muted)
        pdf.drawRightString(plot_left - 9, y_position - 3, str(tick_value))

    points = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for index, (label, value) in enumerate(zip(labels, values)):
        x_position = plot_left + (plot_right - plot_left) * index / max(1, len(values) - 1)
        y_position = plot_bottom + plot_height * value / upper_bound
        points.append((x_position, y_position))
        month_number = int(label[-2:]) if label[-2:].isdigit() else index + 1
        pdf.setFillColor(muted)
        pdf.drawCentredString(x_position, plot_bottom - 17, month_names[month_number - 1])
        pdf.setFillColor(ink)
        pdf.drawCentredString(x_position, min(plot_top + 5, y_position + 9), f"{value:g}")

    pdf.setStrokeColor(green)
    pdf.setLineWidth(2)
    for start, end in zip(points, points[1:]):
        pdf.line(start[0], start[1], end[0], end[1])
    for x_position, y_position in points:
        pdf.setFillColor(green)
        pdf.circle(x_position, y_position, 3.5, fill=1, stroke=0)

    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString((plot_left + plot_right) / 2, plot_bottom - 39, "Month")
    pdf.saveState()
    pdf.translate(margin + 4, (plot_bottom + plot_top) / 2)
    pdf.rotate(90)
    pdf.drawCentredString(0, 0, "Employees")
    pdf.restoreState()

    pdf.setStrokeColor(grid)
    pdf.line(margin, 105, page_width - margin, 105)
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    for index, line in enumerate(textwrap.wrap(limitation, width=118)[:2]):
        pdf.drawString(margin, 89 - index * 12, line)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(margin, 40, "Source: employees_messy join_date records. Unambiguous joining months only.")
    pdf.drawRightString(page_width - margin, 40, "1")
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _create_status_count_pdf(chart, explanation, methodology):
    output = BytesIO()
    page_width, page_height = letter
    pdf = pdf_canvas.Canvas(output, pagesize=letter)
    pdf.setTitle("Employee Count by Status")
    pdf.setAuthor("MARY")

    margin = 48
    green = colors.HexColor("#176344")
    ink = colors.HexColor("#24372d")
    muted = colors.HexColor("#69786f")
    grid = colors.HexColor("#e0e8e2")
    palette = [
        colors.HexColor(color)
        for color in (
            "#176344", "#3d7791", "#b57334", "#79639b", "#bb5360", "#65814e",
            "#ac8c28", "#397f75", "#945e42", "#69778d", "#8b7380",
        )
    ]

    pdf.setFillColor(green)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(margin, page_height - 42, "MARY  |  EMPLOYEE INSIGHTS")
    pdf.setStrokeColor(grid)
    pdf.line(margin, page_height - 54, page_width - margin, page_height - 54)
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 21)
    pdf.drawString(margin, page_height - 88, "Employee count by status")

    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(muted)
    for index, line in enumerate(textwrap.wrap(explanation, width=112)[:2]):
        pdf.drawString(margin, page_height - 109 - index * 13, line)

    labels = [str(value) for value in chart.get("labels", [])]
    values = [float(value) for value in chart.get("values", [])]
    total = sum(values)
    if not labels or len(labels) != len(values) or total <= 0:
        raise ValueError("Status pie chart requires positive counts for each status label.")

    center_x, center_y, radius = 205, 442, 142
    start_angle = 90
    for index, value in enumerate(values):
        extent = value / total * 360
        pdf.setFillColor(palette[index % len(palette)])
        pdf.wedge(
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
            startAng=start_angle,
            extent=extent,
            fill=1,
            stroke=1,
        )
        start_angle += extent

    legend_x = 380
    legend_top = 590
    row_height = min(25, 300 / len(labels))
    pdf.setFont("Helvetica", 8)
    for index, (label, value) in enumerate(zip(labels, values)):
        y_position = legend_top - index * row_height
        pdf.setFillColor(palette[index % len(palette)])
        pdf.rect(legend_x, y_position - 2, 9, 9, fill=1, stroke=0)
        pdf.setFillColor(ink)
        pdf.drawString(legend_x + 15, y_position, label[:22])
        percentage = value / total * 100
        pdf.setFillColor(muted)
        pdf.drawRightString(page_width - margin, y_position, f"{value:,.0f}  ({percentage:.1f}%)")

    pdf.setStrokeColor(grid)
    pdf.line(margin, 115, page_width - margin, 115)
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica", 8)
    for index, line in enumerate(textwrap.wrap(methodology, width=118)[:2]):
        pdf.drawString(margin, 98 - index * 12, line)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(margin, 40, f"Source: employees_messy status values. Total: {total:,.0f} source records.")
    pdf.drawRightString(page_width - margin, 40, "1")
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _normalize_join_date_values(value):
    if isinstance(value, datetime):
        return [value.date().isoformat()]
    if isinstance(value, date):
        return [value.isoformat()]
    normalized = report_helpers.normalize_date_values(value)
    if not isinstance(value, str):
        return normalized

    numeric_date = re.fullmatch(r"\s*(\d{1,4})[/-](\d{1,4})[/-](\d{1,4})\s*", value)
    if not numeric_date:
        return normalized

    parts = numeric_date.groups()
    candidates = list(normalized)
    for year_index, year_text in enumerate(parts):
        if len(year_text) != 4:
            continue
        year = int(year_text)
        date_parts = [int(part) for index, part in enumerate(parts) if index != year_index]
        for day, month in (date_parts, date_parts[::-1]):
            try:
                candidates.append(date(year, month, day).isoformat())
            except ValueError:
                continue
    return list(dict.fromkeys(candidates))


def _extract_join_date_request(requirement):
    if not re.search(
        r"\b(?:join(?:ed|ing)?|hire(?:d|ing)?)\b|"
        r"(?<!\w)(?:join|joining|hire|hiring)[_\s-]*date(?!\w)",
        requirement,
        re.IGNORECASE,
    ):
        return None
    date_match = re.search(
        r"(?<!\d)(\d{1,4}[-/]\d{1,4}[-/]\d{1,4}|"
        r"\d{1,2}[- ]+[A-Za-z]{3,9}[-, ]+\d{4}|"
        r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})(?!\d)",
        requirement,
        re.IGNORECASE,
    )
    if not date_match:
        return None
    raw_value = date_match.group(1)
    normalized_values = _normalize_join_date_values(raw_value)
    if not normalized_values:
        return None
    return raw_value, normalized_values


def _extract_salary_range(requirement):
    lowered = str(requirement or "").casefold()
    if not re.search(r"\b(?:salary|salaries|pay|compensation)\b", lowered):
        return None
    amount = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmb]?)"
    match = re.search(
        rf"\bbetween\s+{amount}\s*(?:-|–|—|to|and)\s*{amount}(?!\w)",
        lowered,
    )
    if not match:
        return None

    def convert(number, suffix):
        scale = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
        value = float(number.replace(",", "")) * scale
        return int(value) if value.is_integer() else value

    first_value = convert(match.group(1), match.group(2))
    second_value = convert(match.group(3), match.group(4))
    return min(first_value, second_value), max(first_value, second_value)


def _run_mary_override(requirement):
    text = " ".join(str(requirement or "").split())
    lowered = text.casefold()
    schema = chatbot.DEFAULT_SCHEMA
    table = f"{schema}.employees_messy"
    is_count = bool(re.search(r"\b(?:count|number|how many)\b", lowered))
    is_line_chart = bool(
        re.search(r"\bline\s+charts?\b", lowered)
        and re.search(r"\b(?:salary|pay)\b", lowered)
        and re.search(r"\b(?:average|avg|mean)\b", lowered)
        and re.search(r"\b(?:department|dept)\b", lowered)
    )
    is_join_year_count = bool(
        is_count
        and re.search(r"\b(?:join(?:ed|ing)?|hire(?:d|ing)?)\b", lowered)
        and (
            re.search(r"\b(?:year|yearly|annual)\b", lowered)
            or re.search(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", lowered)
        )
    )
    year_match = re.search(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", text)
    requested_year = year_match.group(0) if year_match else None
    is_department_count = bool(
        is_count
        and re.search(r"\bby\s+(?:the\s+)?(?:department|dept)\b", lowered)
    )
    is_active_count = bool(
        is_count
        and re.search(r"\bactive\b", lowered)
        and not is_join_year_count
        and not is_department_count
    )
    details_request = bool(re.search(r"\b(?:show|list|display|give|provide)\b", lowered))
    status_condition = re.search(
        r"\bstatus\s+(?:is|equals?|=)\s*[\"']?([a-z][a-z0-9 _-]*)",
        text,
        re.IGNORECASE,
    )
    if status_condition:
        status_value = re.split(r"\b(?:and|but|with|where)\b|[?.!,;]", status_condition.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip()
    else:
        status_value = ""
    is_conditional_details = bool(
        details_request
        and status_condition
        and status_value
        and re.search(r"\b(?:employee|employees|details?|records?)\b", lowered)
    )
    join_date_request = _extract_join_date_request(text)
    is_join_year_details = bool(
        requested_year
        and not is_count
        and details_request
        and re.search(r"\b(?:join|joined|joining|hire|hired|hiring)\b", lowered)
        and re.search(r"\b(?:employee|employees|staff|details?|records?)\b", lowered)
    )
    is_monthly_joiner_report = bool(
        requested_year
        and re.search(r"\bmonthly\b", lowered)
        and re.search(r"\b(?:joiners?|hiring|hires?)\b", lowered)
        and re.search(r"\b(?:line\s+)?charts?\b", lowered)
        and re.search(r"\b(?:report|pdf)\b", lowered)
    )
    salary_range = _extract_salary_range(text)
    is_salary_details = bool(
        salary_range
        and not is_count
        and re.search(r"\b(?:employee|employees|staff|people|workers|details?|records?)\b", lowered)
    )
    is_status_count_report = bool(
        re.search(r"\bstatus\b", lowered)
        and re.search(r"\b(?:count|number|how many)\b", lowered)
        and re.search(r"\bpie\b", lowered)
        and re.search(r"\b(?:report|pdf)\b", lowered)
    )

    try:
        if is_salary_details:
            minimum_salary, maximum_salary = salary_range
            sql = (
                f"SELECT {chatbot.EMPLOYEE_COLUMNS} FROM {table} "
                "WHERE salary BETWEEN :min_salary AND :max_salary "
                "ORDER BY salary, emp_id"
            )
            result = chatbot.execute_query(
                sql,
                select_only=True,
                max_rows=None,
                parameters={"min_salary": minimum_salary, "max_salary": maximum_salary},
            )
            result["question"] = text
            row_count = len(result.get("rows", []))
            explanation = (
                f"Found {row_count:,} employee source records with salary between "
                f"{minimum_salary:,.0f} and {maximum_salary:,.0f}, inclusive. "
                "All matching records are shown."
            )
            analysis = {
                "answer": explanation,
                "tables": [{
                    "title": "Employee details within the salary range",
                    "columns": result["columns"],
                    "rows": result.get("rows", []),
                }],
                "insights": [],
                "limitations": [],
            }
            return _build_analysis_response(text, result, explanation, analysis, ["records"])

        if is_status_count_report:
            sql = f"SELECT status FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            status_index = result["columns"].index("status")
            counts = Counter()
            status_names = {
                "active": "Active",
                "inactive": "Inactive",
                "terminated": "Terminated",
                "probation": "Probation",
                "prob": "Prob",
                "on leave": "On Leave",
                "leave": "Leave",
            }
            for row in result["rows"]:
                value = row[status_index]
                if value is None or not str(value).strip():
                    label = "Missing status"
                else:
                    normalized = " ".join(str(value).split()).casefold()
                    label = status_names.get(normalized)
                    if label is None:
                        raw_label = " ".join(str(value).split())
                        label = raw_label.upper() if len(raw_label) == 1 else raw_label.title()
                counts[label] += 1
            status_rows = [
                [label, count]
                for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
            ]
            total = sum(counts.values())
            chart = {
                "type": "pie",
                "title": "Employee count by status",
                "metric": "Source records",
                "dimension": "Status",
                "unit": "records",
                "labels": [row[0] for row in status_rows],
                "values": [row[1] for row in status_rows],
                "x_label": "Status",
                "y_label": "Source records",
            }
            explanation = (
                f"The pie chart shows the distribution of {total:,} source records by status. "
                "Case variants are grouped, blank values are shown as Missing status, and single-letter codes remain separate."
            )
            methodology = (
                "Counts include every source row, including rows under reused employee IDs. Case-only variants are combined; "
                "single-letter status codes are kept separate rather than assigned an assumed meaning."
            )
            analysis = {
                "answer": explanation,
                "tables": [{
                    "title": "Source record count by status",
                    "columns": ["Status", "Source records"],
                    "rows": status_rows,
                }],
                "insights": [],
                "limitations": [methodology],
                "chart": chart,
            }
            report = {
                "title": "Employee Count by Status",
                "question": "Employee count by status",
                "answer": explanation,
                "methodology": methodology,
                "chart": chart,
                "tables": [],
                "pdf": _create_status_count_pdf(chart, explanation, methodology),
            }
            if not report["pdf"].startswith(b"%PDF-") or not report["pdf"].rstrip().endswith(b"%%EOF"):
                raise ValueError("The generated file failed PDF validation.")
            result["question"] = text
            result_id = uuid.uuid4().hex
            report["result_id"] = result_id
            report_id = chatbot.store_report(report)
            context_id = chatbot.store_query_context(
                text,
                {"columns": ["Status", "Source records"], "rows": status_rows, "row_count": len(status_rows), "truncated": False},
                explanation,
                actions=["chart", "report"],
                report_id=report_id,
                report=report,
                result_id=result_id,
                analysis=analysis,
            )
            return jsonify(
                explanation=explanation,
                analysis=analysis,
                requested_actions=["chart", "report"],
                result_id=result_id,
                context_id=context_id,
                report_id=report_id,
                report_url=f"/api/reports/{report_id}.pdf",
                report_title=report["title"],
                report_filename=report["attachment_filename"],
                report_created=True,
                email_status="not_requested",
            )

        if is_monthly_joiner_report:
            sql = f"SELECT {chatbot.EMPLOYEE_COLUMNS} FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            dataset = report_helpers.build_employee_dataset(result)
            chart = chatbot.build_employee_chart(dataset, "monthly_hiring", text)
            if chart is None:
                return jsonify(error=f"I couldn't build a monthly joining chart for {requested_year}."), 422
            chart["title"] = f"Monthly joiners in {requested_year}"
            included_count = int(sum(chart.get("values", [])))
            explanation = (
                f"The monthly chart shows {included_count:,} unique employees with unambiguous joining dates in {requested_year}."
            )
            limitation = (
                "The chart uses unique employee IDs with valid, unambiguous joining dates; reused IDs and dates that cannot be assigned to one month are excluded."
            )
            analysis = {
                "answer": explanation,
                "tables": [],
                "insights": [],
                "limitations": [limitation],
                "chart": chart,
            }
            report = {
                "title": f"Monthly Joiners in {requested_year}",
                "question": text,
                "business_objective": f"Show the monthly joining pattern for {requested_year}.",
                "scope": f"Monthly counts for January through December {requested_year}.",
                "data_source": "Employee joining dates from the employees_messy source table.",
                "methodology": limitation,
                "limitations": [limitation],
                "tables": [],
                "answer": explanation,
                "insights": [],
                "recommendations": [],
                "possible_explanations": [],
                "implications": [],
                "conclusion": explanation,
                "chart": chart,
                "truncated": False,
            }
            pdf_bytes = _create_monthly_joiner_pdf(requested_year, chart, explanation, limitation)
            if not pdf_bytes.startswith(b"%PDF-") or not pdf_bytes.rstrip().endswith(b"%%EOF"):
                raise ValueError("The generated file failed PDF validation.")
            report["pdf"] = pdf_bytes
            result["question"] = text
            result_id = uuid.uuid4().hex
            report["result_id"] = result_id
            report_id = chatbot.store_report(report)
            context_id = chatbot.store_query_context(
                text,
                result,
                explanation,
                actions=["chart", "report"],
                report_id=report_id,
                dataset=dataset,
                report=report,
                result_id=result_id,
                analysis=analysis,
            )
            return jsonify(
                explanation=explanation,
                analysis=analysis,
                requested_actions=["chart", "report"],
                result_id=result_id,
                context_id=context_id,
                report_id=report_id,
                report_url=f"/api/reports/{report_id}.pdf",
                report_title=report["title"],
                report_filename=report["attachment_filename"],
                report_created=True,
                email_status="not_requested",
            )

        if is_join_year_details:
            sql = f"SELECT {chatbot.EMPLOYEE_COLUMNS} FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            date_index = result["columns"].index("join_date")
            matching_rows = []
            for row in result["rows"]:
                candidate_years = {
                    candidate[:4]
                    for candidate in _normalize_join_date_values(row[date_index])
                }
                if requested_year in candidate_years:
                    matching_rows.append(row)
            result["rows"] = matching_rows
            result["row_count"] = len(matching_rows)
            result["truncated"] = False
            result["question"] = text
            record_label = "source record" if len(matching_rows) == 1 else "source records"
            explanation = (
                f"Found {len(matching_rows):,} {record_label} with a joining year of {requested_year}. "
                "All matching source rows are shown, including rows under reused employee IDs."
            )
            analysis = {
                "answer": explanation,
                "tables": [{
                    "title": f"Employee details for joining year {requested_year}",
                    "columns": result["columns"],
                    "rows": matching_rows,
                }],
                "insights": [],
                "limitations": [],
            }
            return _build_analysis_response(text, result, explanation, analysis, ["records"])

        if (
            join_date_request
            and not is_join_year_count
            and not is_line_chart
            and not is_department_count
            and not is_active_count
        ):
            raw_date, target_dates = join_date_request
            count_request = is_count
            columns = ["join_date"] if count_request else chatbot.requested_person_columns(text)
            if not count_request and "join_date" not in columns:
                columns.append("join_date")
            sql = f"SELECT {', '.join(columns)} FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            date_index = result["columns"].index("join_date")
            target_values = set(target_dates)
            matching_rows = [
                row for row in result["rows"]
                if target_values.intersection(_normalize_join_date_values(row[date_index]))
            ]
            match_note = ""
            if len(target_dates) > 1:
                match_note = (
                    " The numeric date is ambiguous; both "
                    f"{', '.join(sorted(target_dates))} interpretations were checked."
                )
            if count_request:
                count = len(matching_rows)
                explanation = f"Found {count:,} source records for joining date {raw_date}.{match_note}"
                analysis = {
                    "answer": explanation,
                    "tables": [{
                        "title": "Records by joining date",
                        "columns": ["Joining date", "Source records"],
                        "rows": [[raw_date, count]],
                    }],
                    "insights": [],
                    "limitations": [],
                }
                result = {
                    "columns": ["join_date", "source_record_count"],
                    "rows": [[raw_date, count]],
                    "row_count": 1,
                    "truncated": False,
                    "question": text,
                }
            else:
                result["rows"] = matching_rows
                result["row_count"] = len(matching_rows)
                result["truncated"] = False
                result["question"] = text
                record_label = "source record" if len(matching_rows) == 1 else "source records"
                explanation = (
                    f"Found {len(matching_rows):,} {record_label} for joining date {raw_date}.{match_note}"
                    if matching_rows
                    else f"No employee records matched joining date {raw_date}.{match_note}"
                )
                analysis = {
                    "answer": explanation,
                    "tables": [{
                        "title": f"Employee records for joining date {raw_date}",
                        "columns": result["columns"],
                        "rows": matching_rows,
                    }],
                    "insights": [],
                    "limitations": [],
                }
            return _build_analysis_response(text, result, explanation, analysis, ["records"])

        if is_line_chart:
            sql = f"SELECT emp_id, department, salary FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            dataset = report_helpers.build_employee_dataset(result)
            metrics = sorted(
                dataset["department_metrics"],
                key=lambda item: item["department"].casefold(),
            )
            metrics = [item for item in metrics if item["average_salary"] is not None]
            labels = [item["department"] for item in metrics]
            values = [round(item["average_salary"], 2) for item in metrics]
            chart = {
                "type": "line",
                "title": "Average Salary by Department",
                "metric": "Average salary",
                "dimension": "Department",
                "unit": "units as reported",
                "labels": labels,
                "values": values,
                "x_label": "Department (alphabetical order)",
                "y_label": "Average salary",
                "y_unit": "units as reported",
            }
            rows = [[item["department"], item["employee_count"], item["valid_salary_observations"], item["average_salary"]]
                    for item in metrics]
            excluded = dataset["quality"]["rows_with_reused_employee_ids"]
            explanation = (
                f"Average salary by department is shown across {len(metrics)} departments. "
                "The line connects department categories in alphabetical order; it is not a time trend."
            )
            insights = [
                f"A total of {dataset['quality']['valid_salary_observations']:,} positive, finite salary observations were used."
            ]
            if excluded:
                insights.append(
                    f"{excluded:,} source rows under reused employee IDs were excluded from employee-level averages."
                )
            analysis = {
                "answer": explanation,
                "tables": [{
                    "title": "Average salary by department",
                    "columns": ["Department", "Eligible employees", "Valid salaries", "Average salary"],
                    "rows": rows,
                }],
                "insights": insights,
                "limitations": ["Salary averages use eligible employee IDs and positive, finite salary values only."],
                "chart": chart,
            }
            result["question"] = text
            return _build_analysis_response(text, result, explanation, analysis, ["chart", "records"])

        if is_join_year_count:
            sql = f"SELECT join_date FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            date_index = result["columns"].index("join_date")
            counts = Counter()
            ambiguous = 0
            invalid = 0
            missing = 0
            missing_values = report_helpers.MISSING_VALUES
            for row in result["rows"]:
                value = row[date_index]
                if value is None or (isinstance(value, str) and value.strip().casefold() in missing_values):
                    missing += 1
                    continue
                parsed = _normalize_join_date_values(value)
                candidate_years = {candidate[:4] for candidate in parsed}
                if len(candidate_years) == 1:
                    counts[candidate_years.pop()] += 1
                elif candidate_years:
                    ambiguous += 1
                else:
                    invalid += 1
            if requested_year:
                year_count = counts.get(requested_year, 0)
                explanation = (
                    f"Found {year_count:,} source records with a joining year of {requested_year}. "
                    "Rows under reused employee IDs are counted separately."
                )
                analysis = {
                    "answer": explanation,
                    "tables": [{
                        "title": f"Source records joined in {requested_year}",
                        "columns": ["Join year", "Source records"],
                        "rows": [[requested_year, year_count]],
                    }],
                    "insights": [],
                    "limitations": [],
                }
                result["question"] = text
                return _build_analysis_response(text, result, explanation, analysis, ["records"])
            chart = None
            if re.search(r"\b(?:chart|graph|plot|visuali[sz]e)\b", lowered):
                labels = sorted(counts)
                chart = {
                    "type": "line",
                    "title": "Employee Records by Joining Year",
                    "metric": "Source records",
                    "dimension": "Join year",
                    "unit": "records",
                    "labels": labels,
                    "values": [counts[label] for label in labels],
                    "x_label": "Join year",
                    "y_label": "Source records",
                }
            table_rows = [[year, counts[year]] for year in sorted(counts)]
            valid_year_records = sum(counts.values())
            source_row_count = len(result["rows"])
            reconciliation_rows = [
                ["Assigned to one joining year", valid_year_records],
                ["Ambiguous date", ambiguous],
                ["Invalid date", invalid],
                ["Missing date", missing],
                ["Total source records", source_row_count],
            ]
            explanation = (
                f"Assigned {valid_year_records:,} source records with unambiguous dates across {len(counts)} joining years. "
                f"The remaining {ambiguous:,} ambiguous, {invalid:,} invalid, and {missing:,} missing dates are unassigned. "
                f"These mutually exclusive groups reconcile to {source_row_count:,} total source records, including rows under reused employee IDs."
            )
            analysis = {
                "answer": explanation,
                "tables": [
                    {"title": "Source records by joining year", "columns": ["Join year", "Source records"], "rows": table_rows},
                    {"title": "Join-year reconciliation across all source records", "columns": ["Date classification", "Source records"], "rows": reconciliation_rows},
                ],
                "insights": ["All source rows are counted. Ambiguous, invalid, and missing dates are not assigned to a guessed year."],
                "limitations": [
                    f"Year-specific counts total {valid_year_records:,}; "
                    f"{source_row_count - valid_year_records:,} source records have no single valid year and are itemized in the reconciliation table."
                ],
            }
            if chart:
                analysis["chart"] = chart
            result["question"] = text
            return _build_analysis_response(text, result, explanation, analysis, ["records"] + (["chart"] if chart else []))

        if is_department_count:
            sql = f"SELECT department FROM {table}"
            result = chatbot.execute_query(sql, select_only=True, max_rows=None)
            department_index = result["columns"].index("department")
            counts = Counter()
            raw_labels_requested = chatbot.original_department_labels_requested(text)
            for row in result["rows"]:
                raw_label = row[department_index]
                if raw_label is None or not str(raw_label).strip():
                    label = "Missing department"
                elif raw_labels_requested:
                    label = str(raw_label)
                else:
                    label = report_helpers.normalize_department(raw_label)[0]
                counts[label] += 1
            ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
            table_rows = [[label, count] for label, count in ordered]
            chart = {
                "type": "bar",
                "title": "Source Record Count by Department",
                "metric": "Source records",
                "dimension": "Department",
                "unit": "records",
                "labels": [label for label, _ in ordered],
                "values": [count for _, count in ordered],
                "x_label": "Source records",
                "y_label": "Department",
            }
            explanation = (
                f"Counted {len(result['rows']):,} source records across {len(counts)} "
                f"{'original' if raw_labels_requested else 'normalized'} department labels. "
                "Counts include every source row, including rows under reused employee IDs; they are not distinct-person counts."
            )
            analysis = {
                "answer": explanation,
                "tables": [{"title": "Source records by department", "columns": ["Department", "Source records"], "rows": table_rows}],
                "insights": [
                    "Original department spellings are preserved as separate labels."
                    if raw_labels_requested
                    else "Department aliases are normalized for display; uncertain labels remain separate."
                ],
                "limitations": ["Rows sharing a reused employee ID are intentionally counted separately for source-record totals."],
                "chart": chart,
            }
            result["question"] = text
            return _build_analysis_response(text, result, explanation, analysis, ["chart", "records"])

        if is_active_count:
            sql = f"SELECT COUNT(*) AS source_record_count FROM {table} WHERE UPPER(TRIM(status)) = :status"
            result = chatbot.execute_query(
                sql,
                select_only=True,
                max_rows=None,
                parameters={"status": "ACTIVE"},
            )
            count = int(result["rows"][0][0]) if result.get("rows") else 0
            record_label = "source record" if count == 1 else "source records"
            explanation = f"Found {count:,} {record_label} with active status."
            analysis = {
                "answer": explanation,
                "tables": [{
                    "title": "Active-status records",
                    "columns": ["Status", "Source records"],
                    "rows": [["Active", count]],
                }],
                "insights": [],
                "limitations": [],
            }
            result["question"] = text
            return _build_analysis_response(text, result, explanation, analysis, ["records"])

        if is_conditional_details:
            sql = (
                f"SELECT {chatbot.EMPLOYEE_COLUMNS} FROM {table} "
                "WHERE LOWER(TRIM(COALESCE(status, ''))) = LOWER(:status) "
                "ORDER BY emp_id, join_date"
            )
            result = chatbot.execute_query(
                sql,
                select_only=True,
                max_rows=None,
                parameters={"status": status_value},
            )
            record_label = "source record" if result["row_count"] == 1 else "source records"
            explanation = (
                f"Found {result['row_count']:,} {record_label} where status is {status_value!r}. "
                "All matching source rows are shown, including rows under reused employee IDs."
            )
            analysis = {
                "answer": explanation,
                "tables": [{"title": f"Employee details where status is {status_value}", "columns": result["columns"], "rows": result["rows"]}],
                "insights": [],
                "limitations": [],
            }
            result["question"] = text
            return _build_analysis_response(text, result, explanation, analysis, ["records"])
    except SQLAlchemyError:
        app.logger.exception("MARY targeted analytics query failed")
        return jsonify(error="I couldn't retrieve those employee records just now. Please try again."), 503
    except (KeyError, TypeError, ValueError):
        app.logger.exception("MARY could not prepare the requested analytics result")
        return jsonify(error="I couldn't prepare that result from the available employee data."), 500

    return None


@app.before_request
def route_mary_analytics():
    if request.endpoint != "run_requirement" or request.method != "POST":
        return None
    body = request.get_json(silent=True) or {}
    requirement = body.get("requirement") if isinstance(body, dict) else None
    if not isinstance(requirement, str) or not requirement.strip():
        return None
    return _run_mary_override(requirement)


@app.get("/mary/reports/<report_id>/preview")
def preview_mary_report(report_id):
    report = chatbot._reports.get(report_id)
    if report is None or not isinstance(report.get("pdf"), bytes):
        return jsonify(error="This report is no longer available."), 404
    return send_file(
        BytesIO(report["pdf"]),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=report.get("attachment_filename", "business-report.pdf"),
        max_age=0,
    )


_MARY_PAGE_SCRIPT = r"""
<script>
(() => {
  const form = document.getElementById('query-form');
  const toolbar = document.querySelector('.chat-topbar, .topbar');
  if (!form || !toolbar) return;

    const layoutStyle = document.createElement('style');
    layoutStyle.textContent = `
        .chat-messages { width: min(96%, 1280px) !important; }
        .chat-assistant { width: min(100%, 1200px) !important; }
        .composer-wrap-v2 { width: min(96%, 1280px) !important; }
        .chat-transcript { padding-inline: clamp(14px, 1.5vw, 26px) !important; }
        @media (max-width: 560px) {
            .chat-topbar { gap: 6px !important; padding-inline: 8px !important; }
            #mary-ambient-toggle { width: 34px; min-width: 34px; overflow: hidden; padding: 0 !important; font-size: 0 !important; }
            #mary-ambient-toggle::before { content: '\\266B'; font-size: 16px; }
            #mary-reply-voice { max-width: 76px; margin-left: 4px !important; padding-inline: 4px !important; }
        }
    `;
    document.head.append(layoutStyle);

  const soundButton = document.createElement('button');
  soundButton.type = 'button';
    soundButton.id = 'mary-ambient-toggle';
  soundButton.textContent = 'Ambient sound: On';
  soundButton.setAttribute('aria-pressed', 'true');
  soundButton.setAttribute('aria-label', 'Turn background music off');
  soundButton.style.cssText = 'margin-left:12px;padding:7px 10px;border:1px solid #dfe8e2;border-radius:5px;background:#fff;color:#405449;font:12px "Times New Roman",serif;cursor:pointer';
  toolbar.append(soundButton);

    const replyVoice = document.createElement('select');
    replyVoice.id = 'mary-reply-voice';
    replyVoice.setAttribute('aria-label', 'Spoken response voice');
    replyVoice.title = 'Choose the spoken response voice';
    replyVoice.style.cssText = 'margin-left:8px;padding:7px 8px;border:1px solid #dfe8e2;border-radius:5px;background:#fff;color:#405449;font:12px "Times New Roman",serif';
    replyVoice.innerHTML = '<option value="female">Female</option><option value="male">Male</option>';
    replyVoice.value = localStorage.getItem('mary-reply-voice') || 'female';
    toolbar.append(replyVoice);

    if ('speechSynthesis' in window) {
        const speech = window.speechSynthesis;
        const nativeSpeak = speech.speak.bind(speech);
        speech.speak = (utterance) => {
            const preference = replyVoice.value;
            const voicePattern = preference === 'male'
                ? /\b(male|man|david|mark|guy|daniel|alex|james|tom|fred|ryan|andrew)\b/i
                : /\b(female|woman|samantha|victoria|zira|aria|jenny|susan|ava|sara|linda|karen)\b/i;
            const voices = speech.getVoices();
            const language = (utterance.lang || navigator.language || 'en').split('-')[0].toLowerCase();
            const preferredVoice = voices.find((voice) => voicePattern.test(voice.name) && voice.lang.toLowerCase().startsWith(language))
                || voices.find((voice) => voicePattern.test(voice.name));
            if (preferredVoice) utterance.voice = preferredVoice;
            else utterance.pitch = preference === 'male' ? 0.82 : 1.12;
            nativeSpeak(utterance);
        };
        replyVoice.addEventListener('change', () => {
            localStorage.setItem('mary-reply-voice', replyVoice.value);
        });
    }

  let musicEnabled = localStorage.getItem('mary-ambient-music') !== 'off';
    soundButton.textContent = `Ambient music: ${musicEnabled ? 'On' : 'Off'}`;
    soundButton.setAttribute('aria-pressed', String(musicEnabled));
    soundButton.setAttribute('aria-label', `Turn ambient music ${musicEnabled ? 'off' : 'on'}`);
  let audioContext;
  let masterGain;
  let oscillators = [];
  let stopTimer;
  let isPlaying = false;

  function startMusic() {
    if (!musicEnabled || isPlaying) return;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    audioContext = audioContext || new AudioContextClass();
    audioContext.resume().catch(() => {});
    const now = audioContext.currentTime;
    masterGain = audioContext.createGain();
    masterGain.gain.setValueAtTime(0, now);
    masterGain.gain.linearRampToValueAtTime(0.024, now + 0.8);
    masterGain.connect(audioContext.destination);
    [174.61, 220, 261.63, 349.23].forEach((frequency, index) => {
      const oscillator = audioContext.createOscillator();
      const voiceGain = audioContext.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.value = frequency;
      voiceGain.gain.value = index === 0 ? 0.18 : 0.11;
      oscillator.connect(voiceGain);
      voiceGain.connect(masterGain);
      oscillator.start(now);
      oscillators.push(oscillator);
    });
    isPlaying = true;
    clearTimeout(stopTimer);
    stopTimer = setTimeout(stopMusic, 120000);
  }

  function stopMusic() {
    if (!isPlaying || !audioContext || !masterGain) return;
    const now = audioContext.currentTime;
    masterGain.gain.cancelScheduledValues(now);
    masterGain.gain.setTargetAtTime(0, now, 0.18);
    const voices = oscillators;
    oscillators = [];
    isPlaying = false;
    setTimeout(() => voices.forEach((voice) => {
      try { voice.stop(); } catch {}
      voice.disconnect();
    }), 700);
    clearTimeout(stopTimer);
  }

  function syncThinkingMusic() {
    if (document.querySelector('.chat-thinking')) startMusic();
    else stopMusic();
  }

  function repairChartLabels(root) {
    if (!root.querySelectorAll) return;
    root.querySelectorAll('svg text[textLength]').forEach((label) => {
      label.removeAttribute('textLength');
      label.removeAttribute('lengthAdjust');
      if ((label.textContent || '').length > 17) label.setAttribute('font-size', '9');
    });
  }

    function addReplyAudioButtons(root) {
        const replies = [];
        if (root.matches?.('.chat-message.chat-assistant')) replies.push(root);
        if (root.querySelectorAll) replies.push(...root.querySelectorAll('.chat-message.chat-assistant'));
        replies.forEach((reply) => {
            const text = reply.querySelector('.chat-answer-text');
            const actions = reply.querySelector('.chat-message-actions');
            if (!text || !actions || actions.querySelector('[data-mary-reply-audio]')) return;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'message-action-button mary-reply-audio';
            button.dataset.maryReplyAudio = 'true';
            button.textContent = 'Listen';
            button.title = 'Play this response aloud';
            button.setAttribute('aria-label', 'Play this response aloud');
            button.addEventListener('click', () => {
                if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) return;
                const speech = window.speechSynthesis;
                const wasPlaying = button.dataset.playing === 'true';
                document.querySelectorAll('.mary-reply-audio[data-playing="true"]').forEach((activeButton) => {
                    activeButton.dataset.playing = 'false';
                    activeButton.textContent = 'Listen';
                    activeButton.title = 'Play this response aloud';
                    activeButton.setAttribute('aria-label', 'Play this response aloud');
                });
                speech.cancel();
                if (wasPlaying) return;
                const utterance = new SpeechSynthesisUtterance(text.textContent.trim());
                utterance.lang = navigator.language || 'en-US';
                const resetButton = () => {
                    button.dataset.playing = 'false';
                    button.textContent = 'Listen';
                    button.title = 'Play this response aloud';
                    button.setAttribute('aria-label', 'Play this response aloud');
                };
                utterance.onend = resetButton;
                utterance.onerror = resetButton;
                button.dataset.playing = 'true';
                button.textContent = 'Stop';
                button.title = 'Stop audio playback';
                button.setAttribute('aria-label', 'Stop audio playback');
                speech.speak(utterance);
            });
            actions.append(button);
        });
    }

    function addPdfPreviewLinks(root) {
        const anchors = [];
        if (root.matches?.('a.report-action-button[download]')) anchors.push(root);
        if (root.querySelectorAll) anchors.push(...root.querySelectorAll('a.report-action-button[download]'));
        anchors.forEach((download) => {
            const match = download.href.match(/\/api\/reports\/([a-f0-9]+)\.pdf(?:$|[?#])/i);
            const actions = download.closest('.report-actions');
            if (!match || !actions || actions.querySelector('[data-mary-pdf-preview]')) return;
            const preview = document.createElement('a');
            preview.className = 'report-action-button';
            preview.href = `/mary/reports/${match[1]}/preview`;
            preview.target = '_blank';
            preview.rel = 'noopener';
            preview.dataset.maryPdfPreview = 'true';
            preview.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.2-6 9.5-6 9.5 6 9.5 6-3.2 6-9.5 6-9.5-6-9.5-6Z"></path><circle cx="12" cy="12" r="2.5"></circle></svg><span>Preview PDF</span>';
            actions.insertBefore(preview, download);
        });
    }

  soundButton.addEventListener('click', () => {
    musicEnabled = !musicEnabled;
    localStorage.setItem('mary-ambient-music', musicEnabled ? 'on' : 'off');
    soundButton.textContent = `Ambient music: ${musicEnabled ? 'On' : 'Off'}`;
    soundButton.setAttribute('aria-pressed', String(musicEnabled));
    soundButton.setAttribute('aria-label', `Turn ambient music ${musicEnabled ? 'off' : 'on'}`);
    if (musicEnabled) syncThinkingMusic();
    else stopMusic();
  });

  form.addEventListener('submit', () => {
    if (form.checkValidity()) startMusic();
  }, true);
  const observer = new MutationObserver((records) => {
        records.forEach((record) => record.addedNodes.forEach((node) => {
            if (node.nodeType === Node.ELEMENT_NODE) {
                repairChartLabels(node);
                addReplyAudioButtons(node);
                addPdfPreviewLinks(node);
            }
    }));
    syncThinkingMusic();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  repairChartLabels(document);
        addReplyAudioButtons(document);
    addPdfPreviewLinks(document);
})();
</script>
"""


@app.after_request
def add_mary_experience(response):
    if request.path == "/" and response.mimetype == "text/html":
        page = response.get_data(as_text=True)
        if "</body>" in page:
            response.set_data(page.replace("</body>", f"{_MARY_PAGE_SCRIPT}</body>", 1))
    return response


if __name__ == "__main__":
    port = int(os.getenv("PORT", os.getenv("MARY_PORT", "5018")))
    app.run(host="0.0.0.0", port=port, debug=False)
