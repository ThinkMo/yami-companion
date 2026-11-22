from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Optional

CATALOG_DIR = Path(__file__).resolve().parents[1] / "data" / "catalog"
TEXTBOOKS_PATH = CATALOG_DIR / "textbooks.json"
DIRECTORIES_PATH = CATALOG_DIR / "directories.json"

SUBJECT_META: list[tuple[str, str]] = [
    ("chinese", "语文"),
    ("math", "数学"),
    ("english", "英语"),
    ("science", "科学"),
]

DEFAULT_SUBJECT_VERSIONS: dict[str, str] = {
    "chinese": "统编版",
    "math": "人教版",
    "english": "人教版 PEP（三年级起点）",
    "science": "教科版",
}

_CACHED_TEXTBOOKS: Optional[list[dict[str, Any]]] = None
_CACHED_DIRECTORIES: Optional[dict[str, Any]] = None


def _load_textbooks() -> list[dict[str, Any]]:
    global _CACHED_TEXTBOOKS
    if _CACHED_TEXTBOOKS is None:
        if TEXTBOOKS_PATH.exists():
            _CACHED_TEXTBOOKS = json.loads(TEXTBOOKS_PATH.read_text(encoding="utf-8"))
        else:
            _CACHED_TEXTBOOKS = []
    return _CACHED_TEXTBOOKS


def _load_directories() -> dict[str, Any]:
    global _CACHED_DIRECTORIES
    if _CACHED_DIRECTORIES is None:
        if DIRECTORIES_PATH.exists():
            _CACHED_DIRECTORIES = json.loads(DIRECTORIES_PATH.read_text(encoding="utf-8"))
        else:
            _CACHED_DIRECTORIES = {}
    return _CACHED_DIRECTORIES


def get_student_textbook_curriculum(
    *,
    grade: int,
    subject_textbooks: dict[str, str],
    semester: str = "upper",
) -> list[dict[str, Any]]:
    """
    Resolve each subject's real verified textbook and unit/lesson directory from
    `textbooks.json` + `directories.json` for the student's grade and series.
    """
    books = _load_textbooks()
    dirs = _load_directories()
    result: list[dict[str, Any]] = []

    for sub_key, sub_cn in SUBJECT_META:
        wanted_series = (subject_textbooks.get(sub_key) or DEFAULT_SUBJECT_VERSIONS[sub_key]).strip()
        # Score candidates matching subject + grade (+ semester + series)
        best_book: Optional[dict[str, Any]] = None
        best_score = -1
        for b in books:
            if not isinstance(b, dict) or b.get("subject") != sub_key:
                continue
            if b.get("grade") != grade:
                continue
            bid = str(b.get("id") or "")
            if bid not in dirs:
                continue
            score = 0
            s_label = str(b.get("seriesLabel") or b.get("series") or "")
            if s_label == wanted_series:
                score += 10
            elif wanted_series in s_label or s_label in wanted_series:
                score += 6
            if b.get("semester") == semester:
                score += 4
            if b.get("directoryState") == "verified":
                score += 2
            if score > best_score:
                best_score = score
                best_book = b

        units_out: list[dict[str, Any]] = []
        book_title = f"{grade}年级{sub_cn}（{wanted_series}）"
        book_id = ""
        if best_book is not None:
            book_id = str(best_book.get("id") or "")
            book_title = str(best_book.get("fileName") or book_title).replace(".pdf", "")
            dir_entry = dirs.get(book_id) or {}
            raw_units = dir_entry.get("units") or []
            for idx, u in enumerate(raw_units, start=1):
                if not isinstance(u, dict):
                    continue
                u_title = str(u.get("title") or f"第{idx}单元").strip()
                raw_lessons = u.get("lessons") or []
                lessons = [
                    str(ls.get("title") if isinstance(ls, dict) else ls).strip()
                    for ls in raw_lessons
                    if ls
                ]
                units_out.append(
                    {
                        "unit_index": idx,
                        "title": u_title,
                        "lessons": lessons[:8],
                        "lesson_count": len(lessons),
                    }
                )

        result.append(
            {
                "subject_key": sub_key,
                "subject_cn": sub_cn,
                "series": wanted_series,
                "book_id": book_id,
                "book_title": book_title,
                "units": units_out,
            }
        )
    return result


def get_catalog_options(*, grade: Optional[int] = None) -> list[dict[str, Any]]:
    """
    Returns authoritative subjects and their verified textbook series from textbooks.json,
    optionally filtered to a specific grade (1..6).
    """
    rows = _load_textbooks()
    by_sub: dict[str, set[str]] = defaultdict(set)
    for item in rows:
        if not isinstance(item, dict):
            continue
        if grade is not None and item.get("grade") != grade:
            continue
        sub = str(item.get("subject") or "")
        label = str(item.get("seriesLabel") or item.get("series") or "").strip()
        if sub and label:
            by_sub[sub].add(label)

    # Fallback if a grade has no entries for a subject (e.g. Grade 1 English in 3-start regions)
    if grade is not None:
        for key, _ in SUBJECT_META:
            if not by_sub[key]:
                for item in rows:
                    if isinstance(item, dict) and item.get("subject") == key:
                        label = str(item.get("seriesLabel") or item.get("series") or "").strip()
                        if label:
                            by_sub[key].add(label)

    return [
        {
            "key": key,
            "label": cn_label,
            "default_version": DEFAULT_SUBJECT_VERSIONS[key],
            "versions": sorted(by_sub.get(key) or {DEFAULT_SUBJECT_VERSIONS[key]}),
        }
        for key, cn_label in SUBJECT_META
    ]


def parse_subject_textbooks(raw: Optional[str]) -> dict[str, str]:
    """Parse Student.textbook_version (either JSON map or legacy single string) into per-subject dict."""
    base = dict(DEFAULT_SUBJECT_VERSIONS)
    if not raw:
        return base
    s = raw.strip()
    if s.startswith("{"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                for key, _ in SUBJECT_META:
                    val = str(parsed.get(key) or "").strip()
                    if val:
                        base[key] = val
                return base
        except Exception:
            pass
    # Legacy plain string (e.g., "人教版" or "北师大版"): apply to math while keeping defaults
    base["math"] = s
    return base


def serialize_subject_textbooks(mapping: dict[str, str]) -> str:
    clean = dict(DEFAULT_SUBJECT_VERSIONS)
    for key, _ in SUBJECT_META:
        val = str(mapping.get(key) or "").strip()
        if val:
            clean[key] = val
    return json.dumps(clean, ensure_ascii=False)
