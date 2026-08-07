"""Запись решений пользователя по группам-кандидатам в data/decisions.json.

Использование: python scripts/record_decisions.py <начальный_индекс> "<ответы>"
Ответы: для каждой группы через запятую: k / del / буква (a, b, ...)
"""
import json
import re
import sys
from pathlib import Path

REPORT = Path("data/reports/duplicates.json")
DECISIONS = Path("data/decisions.json")


def main():
    start = int(sys.argv[1])
    answers = sys.argv[2]
    a = json.loads(REPORT.read_text(encoding="utf-8"))
    groups = a["candidate_groups"]
    decisions = {}
    if DECISIONS.exists():
        decisions = json.loads(DECISIONS.read_text(encoding="utf-8"))
    letters = "abcdefg"
    for part in answers.replace(",", " ").split():
        part = part.strip()
        match = re.match(r"^(\d+)(?::?)([a-gkdel]*)$", part)
        if not match:
            continue
        idx = int(match.group(1)) - 1
        choice = match.group(2).lower()
        g = groups[idx]
        if choice == "k":
            decisions[g["key"]] = {"skip": True, "reason": "оставить все версии"}
        elif choice == "del":
            decisions[g["key"]] = {
                "canonical": g["canonical"],
                "reason": "удалить неканонические (эвристика)",
            }
        elif choice in letters and len(g["members"]) > letters.index(choice):
            keep = g["members"][letters.index(choice)]["id"]
            decisions[g["key"]] = {"canonical": keep, "reason": "выбрано вручную"}
        else:
            print(f"Не понял ответ для группы {idx + 1}: {choice!r}")
    DECISIONS.write_text(
        json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Записано решений: {len(decisions)}")


if __name__ == "__main__":
    main()
