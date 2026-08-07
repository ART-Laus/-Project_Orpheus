"""Показ групп-кандидатов пакетами для ручного ревью.

Использование: python scripts/review_groups.py [начальный_индекс]
"""
import json
import sys
from pathlib import Path

REPORT = Path("data/reports/duplicates.json")
BATCH = 8


def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    a = json.loads(REPORT.read_text(encoding="utf-8"))
    groups = a["candidate_groups"]
    end = min(start + BATCH, len(groups))
    for idx in range(start, end):
        g = groups[idx]
        print(f"[{idx + 1}/{len(groups)}] «{g['group_name']}»  (ключ: {g['key']})")
        letters = "abcdefg"
        for j, m in enumerate(g["members"]):
            canon = "  *канон" if m["id"] == g["canonical"] else ""
            liked = "лайкнут" if m["in_liked"] else "—"
            pls = ", ".join(m["playlists"]) or "—"
            print(
                f"  {letters[j]}) {m['id']} — «{m['name']}» — {m['album']} "
                f"({m['release_date']}) — {liked} — {pls}{canon}"
            )
        print()
    print(f"--- пакет {start // BATCH + 1}: группы {start + 1}..{end} ---")
    print("Ответ: для каждой группы через запятую: k=оставить все, del=удалить неканон, или буква a/b/c = оставить её")


if __name__ == "__main__":
    main()
