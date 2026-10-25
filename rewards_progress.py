"""Read search-point counters through the visible Rewards UI."""

from dataclasses import dataclass
import re

from runtime_context import close_preserving_error


EARN_URL = "https://rewards.bing.com/earn"
_SEARCH_COUNTER = re.compile(r"必应搜索\s*(-?\d+)\s*/\s*(-?\d+)")
_TODAY_POINTS = re.compile(r"今日积分\s*(-?\d+)")


@dataclass(frozen=True)
class SearchProgress:
    current: int
    maximum: int
    today_points: int | None = None


def parse_search_progress(text: str) -> SearchProgress:
    if not isinstance(text, str):
        raise ValueError("积分明细必须是文本。")
    match = _SEARCH_COUNTER.search(text)
    if match is None:
        raise ValueError("积分明细中没有识别到必应搜索进度。")
    current, maximum = map(int, match.groups())
    if current < 0 or maximum <= 0 or current > maximum:
        raise ValueError("必应搜索进度超出有效范围。")
    today_match = _TODAY_POINTS.search(text)
    today_points = int(today_match.group(1)) if today_match else None
    if today_points is not None and today_points < 0:
        raise ValueError("今日积分不能是负数。")
    return SearchProgress(current, maximum, today_points)


def read_search_progress(context, timeout_ms: int = 30000) -> SearchProgress:
    """Use a temporary tab; ordinary UI clicks only, without account APIs."""
    from playwright.sync_api import expect

    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ValueError("timeout_ms 必须是正整数。")
    page = context.new_page()
    try:
        response = page.goto(EARN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or response.status >= 400:
            raise RuntimeError("积分明细页面请求失败。")
        button = page.get_by_role("button", name=re.compile("今日积分"))
        button.wait_for(state="visible", timeout=timeout_ms)
        page.wait_for_function("""() => {
            const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
            return buttons.some(button => {
                const label = [button.textContent, button.getAttribute('aria-label')].join(' ');
                if (!button.getClientRects().length || !label.includes('今日积分')) return false;
                const key = Object.keys(button).find(name => name.startsWith('__reactProps'));
                return typeof button.onclick === 'function' ||
                    (key && typeof button[key]?.onClick === 'function');
            });
        }""", timeout=timeout_ms)
        button.click(timeout=timeout_ms)
        dialog = page.get_by_role("dialog").first
        dialog.wait_for(state="visible", timeout=timeout_ms)
        # The dialog can render its loading state before counters arrive.
        expect(dialog).to_contain_text(_SEARCH_COUNTER, timeout=timeout_ms)
        return parse_search_progress(dialog.inner_text(timeout=timeout_ms))
    finally:
        close_preserving_error(page)
