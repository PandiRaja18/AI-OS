"""Render a run trace as a terminal-styled SVG for the README.

Usage:
    python tools/trace_to_svg.py .aios/traces/<run_id>.jsonl docs/assets/trace.svg

The asset in the repository is generated from a real offline run, so what the
README shows is what the platform actually printed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

# Kind -> (colour, how many of this kind to keep). Order in the trace is kept.
STORY: dict[str, tuple[str, int]] = {
    "run_started": ("#58a6ff", 1),
    "memory_recall": ("#79c0ff", 1),
    "plan": ("#d2a8ff", 1),
    "replan": ("#d2a8ff", 2),
    "run_status": ("#8b949e", 6),
    "dispatch": ("#39c5cf", 5),
    "tool_call": ("#79c0ff", 5),
    "tool_error": ("#d29922", 2),
    "task_retry": ("#d29922", 1),
    "task_degraded": ("#e3b341", 1),
    "task_blocked": ("#f85149", 1),
    "agent_result": ("#3fb950", 2),
    "conflict_detected": ("#f85149", 1),
    "conflict_resolved": ("#3fb950", 1),
    "conflict_escalated": ("#f85149", 1),
    "signoff_requested": ("#e3b341", 1),
    "signoff_recorded": ("#3fb950", 1),
    "memory_promote": ("#79c0ff", 1),
    "report": ("#3fb950", 1),
}

WIDTH = 1000
PAD = 20
TITLE_BAR = 36
LINE_HEIGHT = 19
FONT_SIZE = 12.5
MESSAGE_CHARS = 80
DURATION_X = 978
COLUMN_X = {"seq": 20, "kind": 50, "actor": 194, "message": 322}


def _is_interesting(message: str) -> bool:
    """Keep the scheduler lines that show a decision, not routine single steps."""
    if message.startswith(("reconcile:", "re-querying", "finalize")):
        return True
    return message.startswith("dispatch:") and "," in message


def select(events: list[dict]) -> list[dict]:
    """Keep the events that tell the story, in trace order."""
    remaining = {kind: budget for kind, (_, budget) in STORY.items()}
    chosen = []
    for event in events:
        kind = event["kind"]
        if remaining.get(kind, 0) <= 0:
            continue
        if kind == "run_status" and not _is_interesting(event["message"]):
            continue
        remaining[kind] -= 1
        chosen.append(event)
    return chosen


def render(events: list[dict]) -> str:
    """Build the SVG document."""
    height = TITLE_BAR + PAD + len(events) * LINE_HEIGHT + PAD
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="{FONT_SIZE}">',
        '<rect width="100%" height="100%" rx="8" fill="#0d1117"/>',
        f'<rect width="100%" height="{TITLE_BAR}" rx="8" fill="#161b22"/>',
        f'<rect y="{TITLE_BAR - 8}" width="100%" height="8" fill="#161b22"/>',
        '<circle cx="20" cy="18" r="5" fill="#ff5f57"/>',
        '<circle cx="38" cy="18" r="5" fill="#febc2e"/>',
        '<circle cx="56" cy="18" r="5" fill="#28c840"/>',
        '<text x="78" y="22" fill="#8b949e">'
        'aios run "Prepare the FY26-Q3 quarterly audit review" --offline</text>',
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="8" '
        'fill="none" stroke="#30363d"/>',
    ]

    y = TITLE_BAR + PAD + 4
    for event in events:
        colour = STORY[event["kind"]][0]
        message = event["message"].replace("\n", " ")
        if len(message) > MESSAGE_CHARS:
            message = message[: MESSAGE_CHARS - 1] + "…"
        took = (
            f'<tspan x="{DURATION_X}" text-anchor="end" fill="#484f58">'
            f'{event["duration_ms"]}ms</tspan>'
            if event.get("duration_ms")
            else ""
        )
        lines.append(
            f'<text y="{y}" xml:space="preserve">'
            f'<tspan x="{COLUMN_X["seq"]}" fill="#484f58">{event["seq"]:>3}</tspan>'
            f'<tspan x="{COLUMN_X["kind"]}" fill="{colour}">'
            f'{escape(event["kind"])}</tspan>'
            f'<tspan x="{COLUMN_X["actor"]}" fill="#6e7681">'
            f'{escape(event["actor"])}</tspan>'
            f'<tspan x="{COLUMN_X["message"]}" fill="#c9d1d9">'
            f'{escape(message)}</tspan>'
            f"{took}</text>"
        )
        y += LINE_HEIGHT

    lines.append("</svg>")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    events = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(select(events)), encoding="utf-8")
    print(f"{target} <- {source} ({len(select(events))} of {len(events)} events)")


if __name__ == "__main__":
    main()
