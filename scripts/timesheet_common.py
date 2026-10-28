"""Shared helpers for the timesheet scripts: config, period selection, time formatting."""
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "timesheet.default.json"
USER_DIR = Path(os.environ.get("TEMPO_TIMESHEET_DIR", Path.home() / ".claude" / "tempo-timesheet"))
USER_CONFIG = USER_DIR / "config.json"


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path=None) -> dict:
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    for extra in (USER_CONFIG, Path(path) if path else None):
        if extra and extra.exists():
            cfg = _deep_merge(cfg, json.loads(extra.read_text(encoding="utf-8")))
    return cfg


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def parse_day(value: str) -> date:
    """YYYY-MM-DD, or DD-MM-YYYY / DD.MM.YYYY as people type it."""
    value = value.strip().rstrip(".")
    for fmt_ in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt_).date()
        except ValueError:
            pass
    raise SystemExit(f"Invalid date '{value}' (expected YYYY-MM-DD or DD-MM-YYYY)")


def resolve_period(args, today: date):
    """(first_day, last_day) from argparse args: positional day, --date, --week, --from/--to, --today."""
    if getattr(args, "from_", None):
        start = parse_day(args.from_)
        end = parse_day(args.to) if args.to else today
    elif args.week is not None:
        anchor = parse_day(args.week) if args.week else today
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
    elif args.date or args.day:
        start = end = parse_day(args.date or args.day)
    elif args.today:
        start = end = today
    else:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    if end < start:
        raise SystemExit("Period end is before its start")
    return start, end


def add_period_args(ap):
    ap.add_argument("day", nargs="?", help="a single day (YYYY-MM-DD or DD-MM-YYYY)")
    ap.add_argument("--date", help="a single day")
    ap.add_argument("--week", nargs="?", const="", metavar="DATE",
                    help="calendar week Mon-Sun containing DATE (default: this week)")
    ap.add_argument("--from", dest="from_", metavar="DATE", help="range start")
    ap.add_argument("--to", metavar="DATE", help="range end (default: today)")
    ap.add_argument("--today", action="store_true", help="today only")


def run_dir_for(start: date, end: date) -> Path:
    name = start.isoformat() if start == end else f"{start.isoformat()}_{end.isoformat()}"
    return USER_DIR / name


def tz_of(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone") or "Europe/Zagreb")


def fmt(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def host_matches(host: str, patterns) -> bool:
    """Glob-ish host match: 'example.com' matches itself and subdomains, '*' wildcards anywhere."""
    host = host.lower()
    for pat in patterns:
        pat = pat.lower()
        if "*" in pat:
            if re.fullmatch(re.escape(pat).replace(r"\*", ".*"), host):
                return True
        elif host == pat or host.endswith("." + pat):
            return True
    return False
