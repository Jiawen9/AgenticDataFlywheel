"""Versioned selected-trajectory export and the OPD action cutoff."""

from ..export_markers import effective_action_edit


EXPORT_PROFILE = "corrected_selection_v1"
OPD_PREFIX_PROFILE = "opd_prefix_v1"
PAIRED_EXPORT_PROFILES = (EXPORT_PROFILE, OPD_PREFIX_PROFILE)


def validate_export_policy(profile, cot_policy, *, automatic=False):
    if profile not in (None, *PAIRED_EXPORT_PROFILES) or cot_policy not in (None, "generate", "skip"):
        raise ValueError("不支持的导出或 COT 策略")
    if cot_policy is not None and profile is None:
        raise ValueError("COT 策略需要明确的导出配置")
    if profile == OPD_PREFIX_PROFILE and (cot_policy != "skip" or automatic):
        raise ValueError("OPD 修正前缀仅用于人工修正且须跳过 COT")


def opd_cutoff_row(rows, row_edits):
    """Return the first retained action changed in the saved correction draft."""
    for row in rows:
        edit = row_edits.get(str(row["excel_row"]), {})
        if not edit.get("deleted") and effective_action_edit(row, edit):
            return row
    return None
