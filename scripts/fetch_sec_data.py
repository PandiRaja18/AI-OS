"""Download SEC Financial Statement Data Sets and build labelled scenarios.

    python scripts/fetch_sec_data.py --quarters 2025q1 2025q2 --email you@org.com
    python scripts/fetch_sec_data.py --quarters 2025q1 --email you@org.com --build

Source page:
    https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets
Archive URL pattern (verified against that page):
    https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{n}.zip
Coverage: 2009Q1 onward, updated quarterly. US government work, public domain.

The SEC asks automated clients to identify themselves with a contact address in
the User-Agent header, so `--email` is required rather than optional.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.datasets import sec  # noqa: E402

BASE_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets"
EXPECTED_MEMBERS = {"sub.txt", "num.txt", "pre.txt", "tag.txt"}
CHUNK = 1 << 20


def quarter_url(quarter: str) -> str:
    return f"{BASE_URL}/{quarter.lower()}.zip"


def download(quarter: str, target: Path, email: str, timeout: float = 120.0) -> Path:
    """Fetch one quarterly archive, skipping it if already present and valid."""
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{quarter.lower()}.zip"
    if path.exists() and verify(path):
        print(f"  have   {path.name} ({path.stat().st_size // 1024} KB)")
        return path

    headers = {"User-Agent": f"AI-OS research harness ({email})"}
    url = quarter_url(quarter)
    print(f"  get    {url}")
    with httpx.stream(
        "GET", url, headers=headers, timeout=timeout, follow_redirects=True
    ) as response:
        if response.status_code == 404:
            raise SystemExit(f"{quarter} is not published at {url}")
        response.raise_for_status()
        with path.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK):
                handle.write(chunk)
    print(f"  saved  {path.name} ({path.stat().st_size // 1024} KB)")
    if not verify(path):
        raise SystemExit(f"{path.name} does not contain the expected members")
    return path


def verify(path: Path) -> bool:
    """A readable ZIP holding the four data files."""
    try:
        with zipfile.ZipFile(path) as bundle:
            members = {name.lower() for name in bundle.namelist()}
    except (zipfile.BadZipFile, OSError):
        return False
    return EXPECTED_MEMBERS.issubset(members)


def summarise(path: Path, limit: int) -> dict:
    """Build scenarios from an archive and report what came out."""
    scenarios = sec.build_scenarios(path, limit=limit)
    counts = scenarios.by_class()
    dependent = sum(1 for item in scenarios.scenarios if item.has_dependent_sources)
    return {
        "archive": path.name,
        "scenarios": len(scenarios.scenarios),
        "by_class": counts,
        "dev": len(scenarios.split("dev")),
        "test": len(scenarios.split("test")),
        "with_dependent_sources": dependent,
        "set": scenarios,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quarters", nargs="+", required=True, help="e.g. 2025q1 2025q2"
    )
    parser.add_argument(
        "--email", required=True, help="contact address for the SEC User-Agent"
    )
    parser.add_argument("--out", default="data/sec", help="where to put the archives")
    parser.add_argument("--build", action="store_true", help="also build scenarios")
    parser.add_argument("--limit", type=int, default=500, help="scenarios per quarter")
    args = parser.parse_args()

    target = Path(args.out)
    archives = []
    print(f"SEC Financial Statement Data Sets -> {target}")
    for quarter in args.quarters:
        archives.append(download(quarter, target, args.email))

    if not args.build:
        print("\nArchives ready. Re-run with --build to generate scenarios.")
        return 0

    out_dir = target / "scenarios"
    out_dir.mkdir(parents=True, exist_ok=True)
    print("\nBuilding scenarios")
    for archive in archives:
        report = summarise(archive, args.limit)
        scenario_set = report.pop("set")
        path = out_dir / f"{scenario_set.name}.json"
        path.write_text(scenario_set.model_dump_json(indent=2), encoding="utf-8")
        print(f"  {report['archive']}: {report['scenarios']} scenarios -> {path.name}")
        print(f"    dev/test      {report['dev']}/{report['test']}")
        print(f"    by class      {json.dumps(report['by_class'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
