"""Observe the website's Edge browsing progress without inferring credit from time."""

from dataclasses import dataclass
from datetime import date
import logging
from time import monotonic

from playwright.sync_api import TimeoutError as BrowserTimeout

from modern_dashboard import decode_flight_records


EARN_URL = "https://rewards.bing.com/earn"
EDGE_START_URL = "https://www.bing.com/?FORM=rwbsl1&rwgbopen=1"
LOGGER = logging.getLogger(__name__)


class EdgeInterfaceChanged(RuntimeError):
    pass


class EdgeReadTimeout(EdgeInterfaceChanged):
    """An explicit per-read deadline, distinct from an invalid model or identity."""


@dataclass(frozen=True)
class EdgeProgress:
    current_minutes: int
    target_minutes: int
    completed_today: bool
    available: bool = True


def parse_edge_progress(scripts) -> EdgeProgress | None:
    try:
        records = decode_flight_records(scripts, strict=True)
    except (ValueError, TypeError, RecursionError) as exc:
        raise EdgeInterfaceChanged("Edge 活动的内嵌 JSON 数据无法识别。") from exc
    supported = False
    results = []
    pending = [(record, False) for record in records]
    while pending:
        item, is_streak = pending.pop()
        if isinstance(item, list):
            pending.extend((child, False) for child in item)
            continue
        if not isinstance(item, dict):
            continue
        supported |= (isinstance(item.get("dailySetItems"), list)
                      or isinstance(item.get("activityCards"), list)
                      or isinstance(item.get("streak"), dict))
        known_title = item.get("title") == "DailyCheckIn_Edge_Title"
        if known_title and item.get("partner") != "edge":
            raise EdgeInterfaceChanged("Edge 活动的标题与合作方标识不一致。")
        # Other app metadata also carries partner=edge without being an activity.
        # A partner-only match is meaningful only in the supported streak model.
        if item.get("partner") == "edge" and (known_title or is_streak):
            current, target = item.get("activitiesCompleted"), item.get("activitiesTotal")
            completed, enabled = item.get("isCurrentDayCompleted"), item.get("isEnabled")
            if (type(current) is not int or type(target) is not int or current < 0
                    or target <= 0 or current > target or type(completed) is not bool
                    or type(enabled) is not bool or type(item.get("activationActivityType")) is not int
                    or item["activationActivityType"] != 714):
                raise EdgeInterfaceChanged("Edge 活动的进度、启用状态或活动类型已改变。")
            results.append(EdgeProgress(current, target, completed, enabled))
            supported = True
        pending.extend((child, key == "streak") for key, child in item.items())
    if results:
        if any(result != results[0] for result in results[1:]):
            raise EdgeInterfaceChanged("同一页面出现相互冲突的 Edge 活动进度。")
        return results[0]
    if supported:
        return None
    raise EdgeInterfaceChanged("页面未提供已知的 Rewards 活动数据结构。")


def read_edge_progress(page, timeout_ms, check_page) -> EdgeProgress | None:
    deadline = monotonic() + timeout_ms / 1000
    try:
        response = page.goto(EARN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    except BrowserTimeout as exc:
        raise EdgeReadTimeout("读取 Edge 活动页面已超时。") from exc
    if response is None or response.status >= 400:
        raise EdgeInterfaceChanged("Edge 活动页面请求失败。")
    last_error = None
    while True:
        if monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            raise EdgeReadTimeout("读取 Edge 活动进度已超时。")
        check_page(page)
        try:
            result = parse_edge_progress(page.locator("script:not([src])").all_text_contents())
            if monotonic() >= deadline:
                raise EdgeReadTimeout("读取 Edge 活动进度已超时。")
            return result
        except EdgeInterfaceChanged as exc:
            last_error = exc
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise last_error
        page.wait_for_timeout(min(250, remaining * 1000))


def run_edge_browsing(context, config, report, day, *, check_page,
                      progress_reader=None, clock=None, wait=None, today=None) -> bool:
    clock = clock or monotonic
    today = today or date.today
    maximum = config.get("edge_max_wait_minutes", 60)
    poll = config.get("edge_poll_seconds", 60)
    if type(maximum) is not int or maximum <= 0 or type(poll) is not int or poll <= 0:
        raise EdgeInterfaceChanged("Edge 等待时长和检查间隔必须是正整数。")
    summary = report["edge_browsing"] = dict(minutes_before=None, minutes_after=None,
        target_minutes=None, completed_before=None, completed_after=None,
        waited_seconds=0, status="checking", stopped_reason=None)
    started = clock()
    deadline = started + maximum * 60
    expired = object()
    observer = None
    browsing = None

    def update_elapsed():
        summary["waited_seconds"] = max(0, round(clock() - started, 3)) if browsing is not None else 0

    def network_timeout():
        return min(config.get("timeout_ms", 30000), max(1, int((deadline - clock()) * 1000)))

    def timed_out():
        summary.update(status="timeout", stopped_reason="max_wait_reached")
        LOGGER.info("Edge 等待已达到上限：最后确认 %s/%s 分钟，服务器完成=%s。",
                    summary["minutes_after"], summary["target_minutes"], summary["completed_after"])
        return False

    def check_date():
        if today() != day:
            summary.update(status="interface_changed", stopped_reason="date_changed")
            raise EdgeInterfaceChanged("Edge 活动运行跨越日期，已停止。")

    def observe():
        check_date()
        if clock() >= deadline:
            return expired
        check_page(observer)
        if clock() >= deadline:
            return expired
        try:
            progress = (progress_reader() if progress_reader is not None
                        else read_edge_progress(observer, network_timeout(), check_page))
        except (EdgeReadTimeout, BrowserTimeout):
            check_date()
            check_page(observer)
            if clock() >= deadline:
                return expired
            raise
        if clock() >= deadline:
            return expired
        check_page(observer)
        check_date()
        return expired if clock() >= deadline else progress

    def record(progress, *, initial=False):
        if progress is None or not progress.available:
            summary.update(status="unavailable", stopped_reason="unavailable")
            if progress is not None:
                summary.update(minutes_after=progress.current_minutes,
                    target_minutes=progress.target_minutes, completed_after=progress.completed_today)
            LOGGER.info("Edge 浏览活动当前不可用，未报告完成。")
            return False
        summary.update(minutes_after=progress.current_minutes, target_minutes=progress.target_minutes,
                       completed_after=progress.completed_today)
        LOGGER.info("Edge 浏览进度：%s/%s 分钟，服务器完成=%s。",
                    progress.current_minutes, progress.target_minutes, progress.completed_today)
        if progress.completed_today and progress.current_minutes >= progress.target_minutes:
            summary.update(status="already_completed" if initial else "completed",
                           stopped_reason="already_completed" if initial else "server_confirmed")
            LOGGER.info("Edge 浏览活动已由服务器确认完成（%s）。", summary["status"])
            return True
        return None

    try:
        check_date()
        observer = context.new_page()
        progress = observe()
        if progress is expired:
            return timed_out()
        if progress is not None:
            summary.update(minutes_before=progress.current_minutes, completed_before=progress.completed_today)
        result = record(progress, initial=True)
        if result is not None:
            return result
        if clock() >= deadline:
            return timed_out()
        browsing = context.new_page()
        try:
            response = browsing.goto(EDGE_START_URL, wait_until="domcontentloaded",
                                     timeout=network_timeout())
        except (EdgeReadTimeout, BrowserTimeout):
            check_date()
            check_page(browsing)
            if clock() >= deadline:
                return timed_out()
            raise
        check_page(browsing)
        if response is not None and response.status >= 400:
            raise EdgeInterfaceChanged("Edge 浏览页面请求失败。")
        browsing.bring_to_front()
        summary["status"] = "waiting"
        while True:
            check_date()
            remaining = deadline - clock()
            if remaining <= 0:
                return timed_out()
            seconds = min(poll, remaining)
            if wait is not None:
                wait(seconds)
            else:
                # Keep Playwright's routing and browser-event callbacks running.
                browsing.wait_for_timeout(seconds * 1000)
            update_elapsed()
            check_date()
            check_page(browsing)
            if clock() >= deadline:
                return timed_out()
            progress = observe()
            if progress is expired:
                return timed_out()
            result = record(progress)
            if result is not None:
                return result
    except KeyboardInterrupt:
        summary.update(status="interrupted", stopped_reason="interrupted")
        LOGGER.info("Edge 浏览已中断：最后确认 %s/%s 分钟，服务器完成=%s。",
                    summary["minutes_after"], summary["target_minutes"], summary["completed_after"])
        raise
    except Exception:
        if summary["status"] not in {"interface_changed", "unavailable", "timeout"}:
            summary.update(status="error", stopped_reason="check_failed")
        raise
    finally:
        update_elapsed()
        for owned in (browsing, observer):
            if owned is not None:
                try:
                    owned.close()
                except Exception:
                    pass
