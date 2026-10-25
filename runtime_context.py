"""Configured wall clock; elapsed deadlines continue to use monotonic clocks."""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(value):
    if value is None:
        return
    if not isinstance(value, str) or not value or value.startswith('/'):
        raise ValueError('timezone 必须是 IANA 时区名称，或省略以沿用本机时区。')
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('无法读取 timezone；请使用有效 IANA 名称并安装 requirements.txt。') from exc


def configured_now(config, instant=None):
    zone = config.get('timezone')
    instant = instant or datetime.now().astimezone()
    return instant.astimezone(ZoneInfo(zone)) if zone else instant.astimezone()


def configured_today(config, instant=None):
    return configured_now(config, instant).date()


def close_preserving_error(resource):
    """Preserve SIGTERM/interrupts when systemd also stops the browser children."""
    primary_error = sys.exc_info()[0] is not None
    try:
        resource.close()
    except Exception:
        if not primary_error:
            raise


def schedule_start_boundary(config, at, instant=None):
    """Windows calendar triggers retain offsets, not IANA daylight-saving rules."""
    if not isinstance(at, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', at):
        raise ValueError('时间必须为 HH:MM。')
    zone = config.get('timezone')
    validate_timezone(zone)
    if zone is None:
        return None
    current = configured_now(config, instant)
    offsets = {datetime(year, month, 1, 12, tzinfo=ZoneInfo(zone)).utcoffset()
               for year in (current.year, current.year + 1) for month in range(1, 13)}
    if len(offsets) != 1:
        raise ValueError('Windows 每日触发器不支持此时区的季节性偏移变化；请使用 Asia/Shanghai 或 UTC。')
    hour, minute = map(int, at.split(':'))
    boundary = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if boundary <= current:
        boundary += timedelta(days=1)
    return boundary.isoformat()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read validated schedule settings without opening a browser.')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--at', default='20:00')
    parser.add_argument('--report-start', help='Convert a saved report timestamp and emit the current configured clock.')
    args = parser.parse_args(argv)
    try:
        from rewards import load_config
        config = load_config(args.config)
        if args.report_start is not None:
            started = datetime.fromisoformat(args.report_start)
            if started.tzinfo is None:
                raise ValueError('报告时间必须包含时区偏移。')
            result = dict(started_at=configured_now(config, started).isoformat(),
                          now=configured_now(config).isoformat())
        else:
            result = dict(timezone=config.get('timezone'), start_boundary=schedule_start_boundary(config, args.at))
        print(json.dumps(result))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
