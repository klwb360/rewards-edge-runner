"""Single-account Microsoft Rewards web-task runner; no credit is inferred from clicks."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

from modern_dashboard import parse_modern_dashboard
from runtime_context import close_preserving_error, configured_now, configured_today, validate_timezone


ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
LOGS = ROOT / ".logs"
DASHBOARD_URL = "https://rewards.microsoft.com/"
EARN_URL = "https://rewards.bing.com/earn"
DEFAULTS = dict(channel="msedge", headless=False, max_tasks=12, visit_seconds=8,
                step_seconds=2, max_quiz_steps=20, timeout_ms=30000,
                search_count=10, search_interval_seconds=15,
                search_terms_file="searches.example.txt", verify_search_points=False,
                edge_daily_enabled=False, edge_max_wait_minutes=60, edge_poll_seconds=60,
                timezone=None)


class InterfaceChanged(RuntimeError):
    pass


class NeedsHuman(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


class SearchCreditStopped(RuntimeError):
    pass


class Terminated(KeyboardInterrupt):
    """SIGTERM unwinds the same browser cleanup paths as an interactive interrupt."""


@dataclass(frozen=True)
class Task:
    offer_id: str
    title: str
    url: str
    kind: str
    complete: bool | None
    source: str = "legacy"
    page: str = "dashboard"
    points: int | None = None


def load_config(path: Path) -> dict:
    supplied = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError("配置必须是 JSON 对象，且只能使用 config.json 中的配置项。")
    config = DEFAULTS | supplied
    validate_timezone(config['timezone'])
    if config["channel"] not in ("msedge", "chromium"):
        raise ValueError("channel 只支持 msedge 或 chromium。")
    if type(config["headless"]) is not bool:
        raise ValueError("headless 必须为 true 或 false。")
    if type(config["verify_search_points"]) is not bool:
        raise ValueError("verify_search_points 必须为 true 或 false。")
    if type(config["edge_daily_enabled"]) is not bool:
        raise ValueError("edge_daily_enabled 必须为 true 或 false。")
    if config["edge_daily_enabled"] and (config["channel"] != "msedge" or config["headless"]):
        raise ValueError("Edge 浏览任务需要 channel=msedge 且 headless=false，请使用可见的 Edge。")
    limits = {"max_tasks": (1, 30), "visit_seconds": (1, 60), "step_seconds": (1, 15),
              "max_quiz_steps": (1, 50), "timeout_ms": (5000, 120000),
              "search_count": (0, 40), "search_interval_seconds": (5, 120),
              "edge_max_wait_minutes": (1, 60), "edge_poll_seconds": (1, 300)}
    for name, (low, high) in limits.items():
        if type(config[name]) is not int or not low <= config[name] <= high:
            raise ValueError(f"{name} 必须是 {low} 到 {high} 之间的整数。")
    if not isinstance(config["search_terms_file"], str) or not config["search_terms_file"].strip():
        raise ValueError("search_terms_file 必须是非空文件路径。")
    return config


def allowed_task_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and host in {"bing.com", "www.bing.com", "cn.bing.com",
                             "rewards.bing.com", "rewards.microsoft.com"}
                and not re.search(r"/(redeem|donate|checkout|payment)(/|$)", parsed.path, re.I))
    except (ValueError, TypeError):
        return False


def parse_dashboard(scripts: list[str]) -> dict:
    """Parse embedded JSON only. Never evaluate text taken from a script tag."""
    decoder = json.JSONDecoder()
    for script in scripts:
        match = re.search(r"\b(?:var|let|const)\s+dashboard\s*=\s*", script)
        if match:
            try:
                value, _ = decoder.raw_decode(script[match.end():])
                if isinstance(value, dict) and isinstance(value.get("dailySetPromotions"), dict):
                    return value
            except (ValueError, TypeError):
                continue
    modern = parse_modern_dashboard(scripts)
    if modern is not None:
        return modern | {"__modern": True}
    raise InterfaceChanged("没有识别到每日活动数据；可能需要登录，或微软已修改页面结构。")


def tasks_for_day(dashboard: dict, today: date) -> list[Task]:
    groups = dashboard.get("dailySetPromotions")
    if not isinstance(groups, dict):
        raise InterfaceChanged("每日活动数据格式已改变。")
    entries = []
    for day, promotions in groups.items():
        try:
            parsed_day = datetime.strptime(day, "%m/%d/%Y").date()
        except (ValueError, TypeError):
            continue
        if parsed_day != today or not isinstance(promotions, list):
            continue
        entries.extend(promotions)
    activities = dashboard.get("activityPromotions", [])
    if not isinstance(activities, list):
        raise InterfaceChanged("日常任务数据格式已改变。")
    entries.extend(activities)
    tasks = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("offerId"), str):
            continue
        offer_id = entry["offerId"]
        url = entry.get("destinationUrl", "")
        completed = entry.get("complete")
        if type(completed) is not bool:
            completed = {"true": True, "false": False}.get(completed) if isinstance(completed, str) else None
        title = str(entry.get("title") or offer_id)[:240]
        promotion_type = entry.get("promotionType")
        promotion_type = promotion_type.lower() if isinstance(promotion_type, str) else ""
        hint = (offer_id + " " + promotion_type).lower()
        kind = "manual"
        if allowed_task_url(url) and completed is not None and entry.get("__locked") is not True:
            if "poll" in hint:
                kind = "poll"
            elif "quiz" in hint:
                kind = "quiz"
            elif promotion_type == "urlreward":
                kind = "visit"
        source = "modern" if entry.get("__source") == "modern" else "legacy"
        page = "earn" if source == "modern" and entry.get("__page") == "earn" else "dashboard"
        points = entry.get("points")
        points = points if type(points) is int and points > 0 else None
        task = Task(offer_id, title, url, kind, completed, source, page, points)
        previous = tasks.get(offer_id)
        if previous is not None and previous.complete != task.complete:
            tasks[offer_id] = replace(previous, complete=None, kind="manual")
        else:
            tasks.setdefault(offer_id, task)
    return list(tasks.values())


def completion_status(task: Task, after: list[Task]) -> str:
    return "completed" if any(item.offer_id == task.offer_id and item.complete is True
                              for item in after) else "unverified"


def read_search_terms(path: Path) -> list[str]:
    if path.stat().st_size > 65536:
        raise ValueError("搜索词文件不能超过 64 KiB。")
    terms = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if len(term) > 200:
            raise ValueError("每条搜索词不能超过 200 个字符。")
        terms.setdefault(term.casefold(), term)
    if not terms:
        raise ValueError("搜索词文件为空，请填写你关注的主题，或将 search_count 设为 0。")
    return list(terms.values())


def select_search_terms(terms: list[str], today: date, count: int) -> list[str]:
    if not terms or count <= 0:
        return []
    offset = today.toordinal() * count % len(terms)
    return [terms[(offset + index) % len(terms)] for index in range(min(count, len(terms)))]


def run_searches(context, config: dict, report: dict, day: date, progress_reader=None) -> None:
    summary = report["searches"] = dict(requested=config["search_count"], opened=0,
                                         skipped_today=0, credit="unverified")
    if config["search_count"] == 0:
        return
    progress = progress_reader() if progress_reader is not None else None
    if progress is not None:
        snapshot = dict(current=progress.current, maximum=progress.maximum, today_points=progress.today_points)
        summary.update(quota_before=snapshot, quota_after=snapshot, observed_points_gained=0)
        if progress.current >= progress.maximum:
            summary["stopped_reason"] = "limit_reached"
            return
    terms_path = Path(config["search_terms_file"])
    if not terms_path.is_absolute():
        terms_path = ROOT / terms_path
    terms = select_search_terms(read_search_terms(terms_path), day, config["search_count"])
    summary["available"] = len(terms)
    history_path = STATE / "search-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    if not isinstance(history, dict) or any(
            not isinstance(key, str) or not isinstance(value, list)
            or any(not isinstance(item, str) or re.fullmatch(r"[a-f0-9]{64}", item) is None for item in value)
            for key, value in history.items()):
        raise ValueError("搜索历史格式异常，已停止以防止重复提交。请检查 .state/search-history.json。")
    history = {key: history[key] for key in sorted(history)[-30:]}
    attempted = history.setdefault(day.isoformat(), [])
    started_day = day
    page = context.new_page()
    no_credit = 0
    try:
        if progress_reader is not None:
            page.goto("https://www.bing.com/", wait_until="domcontentloaded", timeout=config["timeout_ms"])
            check_page(page)
        for term in terms:
            fingerprint = sha256(term.casefold().encode("utf-8")).hexdigest()
            if fingerprint in attempted:
                summary["skipped_today"] += 1
                continue
            if configured_today(config) != started_day:
                raise InterfaceChanged("搜索过程中跨越午夜，已停止；请在新日期重新运行。")
            # Persist BEFORE navigation: process interruption must not trigger a retry storm.
            attempted.append(fingerprint)
            STATE.mkdir(parents=True, exist_ok=True)
            temporary = history_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(history, indent=2), encoding="utf-8")
            temporary.replace(history_path)
            if progress_reader is None:
                response = page.goto("https://www.bing.com/search?" + urlencode({"q": term}),
                                     wait_until="domcontentloaded", timeout=config["timeout_ms"])
                check_page(page)
                if response is None or response.status >= 400:
                    raise RuntimeError("Bing 搜索请求失败；当天不自动重试这条搜索词。")
            else:
                search_box = page.locator("#sb_form_q")
                search_box.fill(term)
                search_box.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=config["timeout_ms"])
                check_page(page)
            page.wait_for_timeout(config["search_interval_seconds"] * 1000)
            check_page(page)
            summary["opened"] += 1
            if progress_reader is not None:
                after = progress_reader()
                if after.maximum != progress.maximum or after.current < progress.current:
                    raise SearchCreditStopped("搜索积分上限或日期状态已变化，已停止以便重新检查。")
                no_credit = no_credit + 1 if after.current == progress.current else 0
                progress = after
                summary["quota_after"] = dict(current=after.current, maximum=after.maximum, today_points=after.today_points)
                summary["observed_points_gained"] = after.current - summary["quota_before"]["current"]
                if summary["observed_points_gained"] > 0:
                    summary["credit"] = "observed"
                logging.info("Bing 搜索进度 %s/%s，本轮观察到增加 %s 分。", after.current, after.maximum, summary["observed_points_gained"])
                if after.current >= after.maximum:
                    summary["stopped_reason"] = "limit_reached"
                    break
                if no_credit >= 2:
                    summary["stopped_reason"] = "no_credit"
                    raise SearchCreditStopped("连续两次搜索未观察到计分，已停止；请稍后查看账户中的计分说明。")
            else:
                logging.info("Bing 搜索页面已打开 %s/%s；积分是否到账未确认。", summary["opened"], len(terms))
    finally:
        close_preserving_error(page)


@contextmanager
def run_lock(path: Path):
    """OS file lock: released automatically even if the Python process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning("已有一个实例在运行，请先关闭它。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def check_page(page) -> None:
    host = urlsplit(page.url).hostname or ""
    if host in {"login.live.com", "login.microsoftonline.com", "account.live.com"}:
        raise NeedsHuman("登录已失效或需要账户验证，请运行 login 手动处理。")
    for selector in ('iframe[src*="arkoselabs"]', 'iframe[src*="recaptcha"]',
                     '#b_captcha', '#captcha', '#challenge-form', '#enforcementFrame'):
        if page.locator(selector).first.is_visible():
            raise NeedsHuman("检测到验证页面，已停止；请运行 login 手动处理。")


def read_dashboard(page, timeout_ms: int, *, navigate=True, url=None) -> dict:
    if navigate:
        page.goto(url or DASHBOARD_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    for _ in range(max(1, timeout_ms // 500)):
        check_page(page)
        value = page.evaluate("""() => typeof dashboard === 'object' && dashboard !== null
            ? JSON.parse(JSON.stringify(dashboard)) : null""")
        if isinstance(value, dict) and isinstance(value.get("dailySetPromotions"), dict):
            return value
        try:
            return parse_dashboard(page.locator("script:not([src])").all_text_contents())
        except InterfaceChanged:
            if "/about" in urlsplit(page.url).path:
                raise NeedsHuman("当前是未登录的介绍页，请先运行 login 并进入 Rewards 仪表板。")
            page.wait_for_timeout(500)
    raise InterfaceChanged("未识别到每日活动数据；停止运行以便检查页面兼容性。")


def read_task_pages(page, timeout_ms: int, day: date) -> list[Task]:
    dashboard = read_dashboard(page, timeout_ms)
    tasks = tasks_for_day(dashboard, day)
    if dashboard.get("__modern") is True or any(task.source == "modern" for task in tasks):
        earn = read_dashboard(page, timeout_ms, url=EARN_URL)
        combined = dashboard | {"activityPromotions": earn.get("activityPromotions", [])}
        tasks = tasks_for_day(combined, day)
    return tasks


def first_visible(page, selector):
    locator = page.locator(selector)
    for index in range(min(locator.count(), 30)):
        candidate = locator.nth(index)
        if candidate.is_visible() and candidate.is_enabled():
            return candidate
    return None


def open_activity(context, dashboard_page, task: Task, config: dict):
    if task.source == "modern":
        # React attaches credit tracking to the card's click handler. A plain GET
        # to its destination bypasses that handler and cannot complete the offer.
        destination_page = EARN_URL if task.page == "earn" else DASHBOARD_URL
        parsed = urlsplit(dashboard_page.url)
        on_target = parsed.path.rstrip("/") == ("/earn" if task.page == "earn" else "/dashboard")
        if not on_target:
            read_dashboard(dashboard_page, config["timeout_ms"], url=destination_page)
        links = dashboard_page.locator("a").filter(has=dashboard_page.get_by_text(task.title, exact=True))
        if links.count() != 1:
            raise InterfaceChanged("无法唯一定位新版活动卡片，已停止以便检查页面。")
        link = links.first
        if not allowed_task_url(link.get_attribute("href")) or link.get_attribute("target") != "_blank":
            raise InterfaceChanged("新版卡片的目标地址或打开方式已改变，已停止。")
        # Server-rendered links are visible before React finishes hydration.
        # Wait for the real handler to exist; never invoke that handler directly.
        dashboard_page.wait_for_function("""({href, title}) => {
            const element = Array.from(document.querySelectorAll('a')).find(
                a => a.getAttribute('href') === href && a.textContent.includes(title));
            if (!element) return false;
            const key = Object.keys(element).find(k => k.startsWith('__reactProps'));
            return typeof element.onclick === 'function' ||
                (key && typeof element[key]?.onClick === 'function');
        }""", arg={"href": link.get_attribute("href"), "title": task.title},
            timeout=config["timeout_ms"])
        with dashboard_page.expect_popup(timeout=config["timeout_ms"]) as popup:
            link.click(timeout=config["timeout_ms"])
        activity = popup.value
        activity.wait_for_load_state("domcontentloaded", timeout=config["timeout_ms"])
        return activity
    activity = context.new_page()
    try:
        response = activity.goto(task.url, wait_until="domcontentloaded")
        check_page(activity)
        if response is None or response.status >= 400:
            raise RuntimeError("任务页面请求失败。")
        return activity
    except Exception:
        activity.close()
        raise


def answer_activity(page, config: dict) -> None:
    """Best effort for known quiz/poll widgets; unknown controls are left alone."""
    clicked = set()
    for _ in range(config["max_quiz_steps"]):
        check_page(page)
        button = first_visible(page, "#rqStartQuiz, #btStartQuiz, #rqNextQuestion, #rqFinishQuiz, #btNext, #btFinish")
        if button is not None:
            button.click(timeout=config["timeout_ms"])
            page.wait_for_timeout(config["step_seconds"] * 1000)
            continue
        question = page.locator("#rqQuestion, #quizQuestion, .rqQuestion").all_text_contents()
        options = page.locator('[id^="rqAnswerOption"], .btOption, [id^="btoption"]')
        selected = False
        for index in range(min(options.count(), 12)):
            option = options.nth(index)
            if not option.is_visible() or not option.is_enabled():
                continue
            key = (tuple(question), option.get_attribute("id"), option.inner_text(), index)
            if key not in clicked:
                clicked.add(key)
                option.click(timeout=config["timeout_ms"])
                page.wait_for_timeout(config["step_seconds"] * 1000)
                selected = True
                break
        if not selected:
            return


def restrict_navigation(route) -> None:
    request = route.request
    # A popup's first request can precede creation of its Frame. Check document
    # navigation directly rather than accessing request.frame during this race.
    if request.is_navigation_request():
        host = urlsplit(request.url).hostname or ""
        if not allowed_task_url(request.url) and host not in {
                "login.live.com", "login.microsoftonline.com", "account.live.com"}:
            route.abort()
            return
    route.fallback()


def inspect_edge_browsing(page, config: dict, report: dict) -> None:
    from edge_browsing import EdgeInterfaceChanged, read_edge_progress
    try:
        progress = read_edge_progress(page, config["timeout_ms"], check_edge_page)
    except EdgeInterfaceChanged as exc:
        raise InterfaceChanged(str(exc)) from exc
    available = progress is not None and progress.available
    current = progress.current_minutes if available else None
    target = progress.target_minutes if available else None
    completed = progress.completed_today if available else None
    status = "unavailable" if not available else (
        "already_completed" if completed and current >= target else "pending")
    report["edge_browsing"] = dict(minutes_before=current, minutes_after=current,
        target_minutes=target, completed_before=completed, completed_after=completed,
        waited_seconds=0, status=status, stopped_reason="inspection_only")
    if available:
        logging.info("Edge 浏览进度 %s/%s 分钟，今日完成标记 %s；本次仅检查。", current, target, completed)
    else:
        logging.warning("当前账户没有可用的 Edge 浏览任务；本次仅检查。")


def run_edge_phase(context, config: dict, report: dict, day: date) -> bool:
    from edge_browsing import EdgeInterfaceChanged, run_edge_browsing
    try:
        return run_edge_browsing(context, config, report, day, check_page=check_edge_page,
                                today=lambda: configured_today(config))
    except EdgeInterfaceChanged as exc:
        raise InterfaceChanged(str(exc)) from exc


def check_edge_page(page) -> None:
    check_page(page)
    location = urlsplit(page.url)
    if location.hostname in {"rewards.bing.com", "rewards.microsoft.com"} and location.path.rstrip("/") == "/about":
        raise NeedsHuman("Edge 活动已跳转到未登录介绍页，请运行 login 手动恢复登录。")


def browser_options(command, config):
    options = dict(headless=config['headless'] if command != 'login' else False,
                   accept_downloads=False, locale='zh-CN')
    if config['channel'] == 'msedge':
        options['channel'] = 'msedge'
    return options


def run_browser(command: str, config: dict, report: dict) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        options = browser_options(command, config)
        context = playwright.chromium.launch_persistent_context(str(STATE / "profile"), **options)
        context.set_default_timeout(config["timeout_ms"])
        context.on("page", lambda item: item.on("dialog", lambda dialog: dialog.dismiss()))
        if command != "login":
            context.route("**/*", restrict_navigation)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if command == "login":
                if not sys.stdin.isatty():
                    raise NeedsHuman("login 需要交互终端，请在 PowerShell 或 SSH 交互终端运行。")
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
                input("请在 Edge 中手动登录并进入 Rewards 仪表板；完成后回到此窗口按 Enter：")
                read_dashboard(page, config["timeout_ms"], navigate=False)
                report["status"] = "login_saved"
                logging.info("已识别登录后的每日活动数据，会话保存在 .state/profile。")
                return 0
            day = configured_today(config)
            tasks = read_task_pages(page, config["timeout_ms"], day)
            if not tasks and command == "inspect" and not config["edge_daily_enabled"]:
                raise InterfaceChanged("没有找到本机日期对应的每日活动。可能是地区不提供、服务端日期不同或页面改版；未执行任何任务。")
            if not tasks:
                report["reason"] = "未找到本机日期对应的网页活动；继续检查配置的其他阶段。"
                logging.warning("%s", report["reason"])
            for task in tasks:
                report["tasks"].append(dict(offer_id=task.offer_id, title=task.title, kind=task.kind,
                                            page=task.page, points=task.points,
                                            status="already_completed" if task.complete is True else "pending"))
            if command == "inspect":
                report["status"] = "inspected"
                for item in report["tasks"]:
                    logging.info("%s | %s | %s", item["status"], item["kind"], item["title"])
                if config["edge_daily_enabled"]:
                    inspect_edge_browsing(page, config, report)
                return 0
            attempts = 0
            for task, result in zip(tasks, report["tasks"]):
                if task.complete is True:
                    continue
                if task.kind == "manual" or attempts >= config["max_tasks"]:
                    result.update(status="manual", reason="不支持的任务类型、缺少完成标记或超过单次任务上限。")
                    continue
                attempts += 1
                result["status"] = "attempting"
                logging.info("处理 %s: %s", task.kind, task.title)
                activity = None
                try:
                    activity = open_activity(context, page, task, config)
                    check_page(activity)
                    activity.wait_for_timeout(config["visit_seconds"] * 1000)
                    answer_activity(activity, config)
                    check_page(activity)
                except NeedsHuman:
                    result["status"] = "needs_human"
                    raise
                except Exception:
                    result["status"] = "failed"
                    raise
                finally:
                    if activity is not None:
                        close_preserving_error(activity)
                if configured_today(config) != day:
                    raise InterfaceChanged("运行跨越了配置时区午夜，已停止；请在新日期重新运行。")
                for verification in range(3):
                    after = read_task_pages(page, config["timeout_ms"], day)
                    result["status"] = completion_status(task, after)
                    if result["status"] == "completed":
                        break
                    if verification < 2:
                        page.wait_for_timeout(config["step_seconds"] * 1000)
                logging.info("%s: %s", result["status"], task.title)
            progress_reader = None
            if config["verify_search_points"] and config["search_count"]:
                from rewards_progress import read_search_progress
                progress_reader = lambda: read_search_progress(context, config["timeout_ms"])
            search_ok = True
            try:
                run_searches(context, config, report, day, progress_reader=progress_reader)
            except SearchCreditStopped as exc:
                if not config["edge_daily_enabled"] or report.get("searches", {}).get("stopped_reason") != "no_credit":
                    raise
                search_ok = False
                report["reason"] = str(exc)
                logging.warning("%s 将继续检查 Edge 浏览任务。", exc)
            edge_ok = True
            if config["edge_daily_enabled"]:
                edge_ok = run_edge_phase(context, config, report, day)
            complete = bool(tasks) and search_ok and edge_ok and all(
                item["status"] in {"completed", "already_completed"} for item in report["tasks"])
            report["status"] = "completed" if complete else "partial"
            return 0 if complete else 1
        finally:
            close_preserving_error(context)


def doctor(config):
    """Local diagnostics only: never launch a browser or access an account."""
    from importlib.metadata import PackageNotFoundError, version
    checks = [dict(name='Python >= 3.10', ok=sys.version_info >= (3, 10), detail=sys.version.split()[0]),
              dict(name='timezone', ok=True, detail=config.get('timezone') or 'host-local'),
              dict(name='browser locale', ok=True, detail='zh-CN')]
    try:
        dependency = version('playwright')
    except PackageNotFoundError:
        dependency = None
    checks.append(dict(name='playwright', ok=dependency is not None, detail=dependency or 'missing'))
    if config['channel'] == 'msedge':
        candidates = [shutil.which(name) for name in ('microsoft-edge', 'microsoft-edge-stable', 'msedge')]
        if os.name == 'nt':
            candidates += [str(Path(os.environ.get(key, '')) / 'Microsoft/Edge/Application/msedge.exe')
                           for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        edge = next((path for path in candidates if path and Path(path).is_file()), None)
        checks.append(dict(name='Microsoft Edge', ok=edge is not None, detail=edge or 'missing'))
    else:
        checks.append(dict(name='Chromium', ok=False, detail='doctor requires msedge for the supported daily workflow'))
    return dict(ok=all(item['ok'] for item in checks), checks=checks)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Microsoft Rewards 单账户网页版每日活动脚本")
    parser.add_argument("command", choices=["login", "inspect", "run", "doctor"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args(argv)
    LOGS.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(LOGS / "rewards.log", maxBytes=1_000_000,
                                                            backupCount=3, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    report = dict(started_at=datetime.now().astimezone().isoformat(), command=args.command, status="started", tasks=[])
    config = DEFAULTS.copy()
    exit_code = 1
    try:
        config = load_config(args.config)
        report.update(started_at=configured_now(config).isoformat(), timezone=config.get('timezone'))
        if args.command == 'doctor':
            diagnosis = doctor(config)
            report.update(status='diagnosed', diagnostics=diagnosis)
            for item in diagnosis['checks']:
                logging.info('%s: %s (%s)', item['name'], 'OK' if item['ok'] else 'MISSING', item['detail'])
            exit_code = 0 if diagnosis['ok'] else 1
        else:
            with run_lock(STATE / "run.lock"):
                exit_code = run_browser(args.command, config, report)
    except NeedsHuman as exc:
        report.update(status="needs_human", reason=str(exc))
        logging.error("%s", exc)
        exit_code = 2
    except InterfaceChanged as exc:
        report.update(status="interface_changed", reason=str(exc))
        logging.error("%s", exc)
        exit_code = 3
    except SearchCreditStopped as exc:
        report.update(status="search_paused", reason=str(exc))
        logging.warning("%s", exc)
    except Terminated:
        report.update(status='interrupted', reason='收到 SIGTERM，已保留最后观察到的进度。', signal='SIGTERM')
        if 'edge_browsing' in report:
            report['edge_browsing'].update(status='interrupted', stopped_reason='sigterm')
        logging.warning('%s', report['reason'])
        exit_code = 143
    except KeyboardInterrupt:
        report.update(status="interrupted", reason="用户中断了本次运行，已保留最后观察到的进度。")
        logging.warning("%s", report["reason"])
        exit_code = 130
    except (AlreadyRunning, ValueError, OSError) as exc:
        report.update(status="error", reason=str(exc))
        logging.error("%s", exc)
    except Exception as exc:
        # Playwright exceptions may contain navigation URLs. Keep them out of saved logs.
        report.update(status="error", reason=f"{type(exc).__name__}: 浏览器启动或操作失败，请检查网络、Edge 和页面兼容性。")
        logging.error("%s", report["reason"])
    report.update(finished_at=configured_now(config).isoformat(), exit_code=exit_code)
    target = LOGS / (configured_now(config).strftime("%Y%m%d-%H%M%S-%f") + ".json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("报告: %s", target)
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
        handler.close()
    return exit_code


def main(argv=None) -> int:
    previous_umask = os.umask(0o077) if os.name != 'nt' else None
    install_handler = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGTERM) if install_handler else None
    def stop(signum, frame):
        raise Terminated()
    try:
        if install_handler:
            signal.signal(signal.SIGTERM, stop)
        return _main(argv)
    finally:
        if install_handler:
            signal.signal(signal.SIGTERM, previous)
        if previous_umask is not None:
            os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
