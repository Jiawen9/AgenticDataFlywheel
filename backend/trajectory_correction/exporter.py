"""Reproduce the SFT/RL/native routing rules of ``human8.0.py``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from .constants import WORKBOOK_SUFFIXES


def _edit(session: dict[str, Any], row_number: int) -> dict[str, Any]:
    return session.get("row_edits", {}).get(str(row_number), {})


def _active_rows(session: dict[str, Any], group: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in group["rows"] if not _edit(session, int(row["excel_row"])).get("deleted", False)]


def _is_action_edited(session: dict[str, Any], row_number: int) -> bool:
    return "actions" in _edit(session, row_number)


def _is_sop_edited(session: dict[str, Any], row_number: int) -> bool:
    return "sop" in _edit(session, row_number)


def _row_values(
    sheet: Any,
    headers: list[str],
    row_number: int,
    session: dict[str, Any],
    *,
    restored_action: str | None = None,
) -> list[Any]:
    edits = _edit(session, row_number)
    values = [sheet.cell(row_number, index + 1).value for index in range(len(headers))]
    action_name = "actions" if "actions" in headers else "action"
    if action_name in headers:
        values[headers.index(action_name)] = (
            restored_action if restored_action is not None else edits.get("actions", values[headers.index(action_name)])
        )
    if "sop" in headers and "sop" in edits:
        values[headers.index("sop")] = edits["sop"]
    return values


def _append_sheet(workbook: Workbook, sheet_name: str, headers: list[str], rows: list[tuple[list[Any], int]]) -> int:
    sheet = workbook.create_sheet(sheet_name)
    sheet.append(headers)
    refined_index = headers.index("is_refined")
    for values, refined_row in rows:
        values = list(values)
        if len(values) < len(headers):
            values.append(None)
        values[refined_index] = refined_row
        sheet.append(values)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    return len(rows)


def export_session_workbook(
    *,
    workbook_path: Path,
    snapshot: dict[str, Any],
    session: dict[str, Any],
    output_dir: Path,
    export_id: str,
) -> dict[str, Any]:
    if workbook_path.suffix.lower() not in WORKBOOK_SUFFIXES:
        raise ValueError("仅支持 .xlsx 或 .xlsm 文件")
    source = load_workbook(workbook_path, data_only=False, keep_vba=workbook_path.suffix.lower() == ".xlsm")
    try:
        sheet = source.active
        source_headers = [
            str(sheet.cell(1, column).value).strip()
            for column in range(1, sheet.max_column + 1)
            if str(sheet.cell(1, column).value).strip()
        ]
        headers = source_headers if "is_refined" in source_headers else [*source_headers, "is_refined"]

        routed: dict[str, list[tuple[list[Any], int]]] = {
            "SFT_人工精修": [],
            "RL_负向反思": [],
            "原生_完美通过": [],
            "原生_异常待处理": [],
        }
        group_exports = session.get("group_exports", {})
        for group in snapshot["groups"]:
            if not bool(group_exports.get(group["group_id"], group["export"])):
                continue
            rows = _active_rows(session, group)
            action_rows = [row for row in rows if _is_action_edited(session, int(row["excel_row"]))]
            if not action_rows:
                if any(_is_sop_edited(session, int(row["excel_row"])) for row in rows):
                    routed["SFT_人工精修"].extend(
                        (_row_values(sheet, source_headers, int(row["excel_row"]), session), 1 if _is_sop_edited(session, int(row["excel_row"])) else 0)
                        for row in rows
                    )
                else:
                    sheet_name = "原生_完美通过" if group["quality"] == "完成且过程正常" else "原生_异常待处理"
                    routed[sheet_name].extend(
                        (_row_values(sheet, source_headers, int(row["excel_row"]), session), 0)
                        for row in rows
                    )
                continue

            first_action = int(action_rows[0]["excel_row"])
            sft_rows = rows[: next(index for index, row in enumerate(rows) if int(row["excel_row"]) == first_action) + 1]
            routed["SFT_人工精修"].extend(
                (_row_values(sheet, source_headers, int(row["excel_row"]), session), 1 if int(row["excel_row"]) == first_action else 0)
                for row in sft_rows
            )
            if len(action_rows) >= 2:
                second_action = int(action_rows[1]["excel_row"])
                rl_rows = rows[: next(index for index, row in enumerate(rows) if int(row["excel_row"]) == second_action) + 1]
                original_first = _edit(session, first_action).get("original_actions")
                routed["RL_负向反思"].extend(
                    (
                        _row_values(
                            sheet,
                            source_headers,
                            int(row["excel_row"]),
                            session,
                            restored_action=original_first if int(row["excel_row"]) == first_action else None,
                        ),
                        1 if int(row["excel_row"]) == second_action else 0,
                    )
                    for row in rl_rows
                )

        if not any(routed.values()):
            raise ValueError("当前没有符合导出条件的数据；请至少勾选一个任务")

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{workbook_path.stem}_精修与筛查_{timestamp}_{export_id[:6]}.xlsx"
        output_path = output_dir / filename
        exported = Workbook()
        default = exported.active
        exported.remove(default)
        counts: dict[str, int] = {}
        for name, rows in routed.items():
            if rows:
                counts[name] = _append_sheet(exported, name, headers, rows)
        temporary = output_path.with_name(f".{output_path.name}.tmp")
        exported.save(temporary)
        temporary.replace(output_path)
        return {
            "filename": filename,
            "sheets": counts,
            "summary": {"rows": sum(counts.values()), "groups": len(snapshot["groups"])},
        }
    finally:
        source.close()
