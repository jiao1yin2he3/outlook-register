"""Press-hold captcha solver (sync Playwright). Never closes page."""
from __future__ import annotations

import random
import re
import time
from typing import Callable

LogFn = Callable[[str], None]

CAPTCHA_OUTER_IFRAME = (
    'iframe[title="\u9a8c\u8bc1\u8d28\u8be2"], '
    'iframe[title*="\u9a8c\u8bc1"], '
    'iframe[title*="challenge" i], '
    'iframe[data-testid*="captcha" i], '
    'iframe#humanCaptchaIframe, '
    'iframe[src*="hsprotect" i]'
)
CAPTCHA_INNER_IFRAME = 'iframe[style*="display: block"], iframe[style*="display:block"]'

CAPTCHA_PRESS_SELS = [
    '[aria-label="\u53ef\u8bbf\u95ee\u6027\u6311\u6218"]',
    '[aria-label*="\u53ef\u8bbf\u95ee\u6027"]',
    '[aria-label*="\u6309\u4f4f"]',
    '[aria-label*="Press and hold" i]',
    '[aria-label*="Hold" i]',
    '[aria-label*="Press" i]',
    'button:has-text("\u6309\u4f4f")',
    '[role="button"]:has-text("\u6309\u4f4f")',
    'text=\u6309\u4f4f',
]
CAPTCHA_AGAIN_SELS = [
    '[aria-label="\u518d\u6b21\u6309\u4e0b"]',
    '[aria-label*="\u518d\u6b21\u6309\u4e0b"]',
    '[aria-label*="Press again" i]',
    'button:has-text("\u518d\u6b21")',
    'text=\u518d\u6b21\u6309\u4e0b',
]
CAPTCHA_FIELD_TEXTS = {
    "wait": ["\u8bf7\u7a0d\u5019", "Please wait", "\u7a0d\u5019"],
    "retry": ["\u8bf7\u518d\u8bd5\u4e00\u6b21", "Try again", "\u518d\u8bd5\u4e00\u6b21"],
    "holdHint": ["\u957f\u6309\u8be5\u6309\u94ae", "Press and hold", "\u6309\u4f4f"],
    "challenge": ["\u53ef\u8bbf\u95ee\u6027\u6311\u6218", "\u8bc1\u660e\u4f60\u4e0d\u662f\u673a\u5668\u4eba"],
}


def _log_fn(log: LogFn | None) -> LogFn:
    return log or (lambda _m: None)


def is_puzzle_captcha(page) -> bool:
    try:
        if page.locator("iframe#enforcementFrame").count() > 0:
            return True
    except Exception:
        pass
    try:
        blob = ""
        try:
            blob = (page.locator("body").inner_text(timeout=800) or "")[:800]
        except Exception:
            blob = ""
        if re.search(r"FunCaptcha|arkose|enforcementFrame|puzzle|select all", blob, re.I):
            return True
    except Exception:
        pass
    return False


def captcha_frame_locators(page):
    frame1 = page.frame_locator(CAPTCHA_OUTER_IFRAME).first
    frame2 = frame1.frame_locator(CAPTCHA_INNER_IFRAME).first
    return frame1, frame2


def captcha_candidate_frames(page):
    frames = page.frames
    scored = []
    for f in frames:
        u = f.url or ""
        score = 0
        if re.search(r"hsprotect|blob:.*hsprotect|ch_ctx", u, re.I):
            score += 10
        if re.search(r"about:blank", u, re.I):
            score += 3
        if re.search(r"account\.live|login\.live|fpt\.live|crcldu", u, re.I):
            score -= 5
        scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored]


def captcha_has_text(page, frame1, frame2, text: str) -> bool:
    for fr in (frame2, frame1):
        try:
            if fr.get_by_text(text, exact=False).count() > 0:
                return True
        except Exception:
            pass
    try:
        if page.get_by_text(text, exact=False).count() > 0:
            return True
    except Exception:
        pass
    for fr in captcha_candidate_frames(page):
        try:
            t = fr.locator("body").inner_text(timeout=400)
            if t and text in t:
                return True
        except Exception:
            pass
    return False


def captcha_has_any_text(page, frame1, frame2, texts) -> str | None:
    for t in texts:
        if captcha_has_text(page, frame1, frame2, t):
            return t
    return None


def captcha_get_box(page, frame1, frame2, sels):
    list_sels = list(sels) if isinstance(sels, (list, tuple)) else [sels]

    def try_loc(scope, sel, tag):
        try:
            loc = scope.locator(sel).first
            if loc.count() == 0:
                return None
            box = loc.bounding_box()
            if box and box.get("width", 0) > 3 and box.get("height", 0) > 3:
                return {"box": box, "via": tag, "sel": sel}
        except Exception:
            return None
        return None

    for sel in list_sels:
        hit = try_loc(frame2, sel, "frame2")
        if hit:
            return hit
        hit = try_loc(frame1, sel, "frame1")
        if hit:
            return hit
        hit = try_loc(page, sel, "page")
        if hit:
            return hit

    for fr in captcha_candidate_frames(page):
        for sel in list_sels:
            try:
                loc = fr.locator(sel).first
                if loc.count() == 0:
                    continue
                box = loc.bounding_box()
                if box and box.get("width", 0) > 3 and box.get("height", 0) > 3:
                    return {
                        "box": box,
                        "via": f"frame:{(fr.url or '')[:60]}",
                        "sel": sel,
                    }
            except Exception:
                continue

    try:
        btn = page.get_by_role(
            "button", name=re.compile(r"\u6309\u4f4f|Hold|Press and hold", re.I)
        ).first
        if btn.count() > 0:
            box = btn.bounding_box()
            if box and box.get("width", 0) > 3 and box.get("height", 0) > 3:
                return {"box": box, "via": "role-button", "sel": "getByRole(button,\u6309\u4f4f)"}
    except Exception:
        pass
    return None


def _center_with_offset(box, offset_range: int):
    x = box["x"] + box["width"] / 2 + random.randint(-offset_range, offset_range)
    y = box["y"] + box["height"] / 2 + random.randint(-offset_range, offset_range)
    return x, y


def captcha_press_at(page, box, offset_range, label, hold_min, hold_max, log: LogFn | None = None):
    _log = _log_fn(log)
    x, y = _center_with_offset(box, offset_range)
    hold_time = random.randint(hold_min, hold_max)
    _log(f"{label} at {int(x)},{int(y)} hold={hold_time}ms")
    page.mouse.move(x, y, steps=random.randint(3, 8))
    page.wait_for_timeout(random.randint(200, 500))
    page.mouse.down()
    page.wait_for_timeout(hold_time)
    page.mouse.up()
    return hold_time


def wait_captcha_press_ready(page, wait_ms: int = 60000, log: LogFn | None = None) -> bool:
    _log = _log_fn(log)
    deadline = time.time() + wait_ms / 1000.0
    last_log = 0.0

    try:
        next_btn = page.get_by_role(
            "button", name=re.compile(r"\u4e0b\u4e00\u6b65|Next|\u7ee7\u7eed|Continue", re.I)
        ).first
        if next_btn.is_visible(timeout=400) and page.locator(CAPTCHA_OUTER_IFRAME).count() == 0:
            _log("[Bind: CAPTCHA] pre-step click next to load press-hold")
            try:
                next_btn.click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
    except Exception:
        pass

    try:
        page.locator(CAPTCHA_OUTER_IFRAME).first.wait_for(
            state="attached", timeout=min(20000, wait_ms)
        )
        _log("[Bind: CAPTCHA] outer iframe attached")
    except Exception:
        _log("[Bind: CAPTCHA] outer iframe not attached yet")

    while time.time() < deadline:
        frame1, frame2 = captcha_frame_locators(page)
        press_hit = captcha_get_box(page, frame1, frame2, CAPTCHA_PRESS_SELS)
        if press_hit:
            _log(f"[Bind: CAPTCHA] press ready via={press_hit['via']}")
            return True
        again_hit = captcha_get_box(page, frame1, frame2, CAPTCHA_AGAIN_SELS)
        if again_hit:
            _log(f"[Bind: CAPTCHA] again ready via={again_hit['via']}")
            return True
        wait_t = captcha_has_any_text(page, frame1, frame2, CAPTCHA_FIELD_TEXTS["wait"])
        if wait_t:
            _log(f"[Bind: CAPTCHA] wait-text ready: {wait_t}")
            return True
        hold_t = captcha_has_any_text(page, frame1, frame2, CAPTCHA_FIELD_TEXTS["holdHint"])
        if hold_t:
            try:
                outer = page.locator(CAPTCHA_OUTER_IFRAME).first
                ob = outer.bounding_box()
                if ob and ob.get("width", 0) > 20 and ob.get("height", 0) > 20:
                    _log(f"[Bind: CAPTCHA] hold-hint + outer ready: {hold_t}")
                    return True
            except Exception:
                pass

        try:
            if page.locator(CAPTCHA_OUTER_IFRAME).count() == 0:
                next_btn = page.get_by_role(
                    "button", name=re.compile(r"\u4e0b\u4e00\u6b65|Next", re.I)
                ).first
                if next_btn.is_visible(timeout=300):
                    next_btn.click(timeout=3000)
        except Exception:
            pass

        if time.time() - last_log > 8:
            last_log = time.time()
            _log("[Bind: CAPTCHA] waiting for press control...")
        page.wait_for_timeout(300)

    _log(f"[Bind: CAPTCHA] press control not ready after {wait_ms}ms")
    return False


def handle_press_hold_captcha(
    page,
    *,
    max_rounds: int = 3,
    deadline_ms: int = 120_000,
    log: LogFn | None = None,
) -> bool:
    """Attempt press-hold captcha. Returns True if challenge cleared, False otherwise.

    Puzzle/FunCaptcha is not solved — returns False (runner maps to captcha status).
    Never closes the page/browser.
    """
    _log = _log_fn(log)
    started = time.time()

    def check_deadline():
        if (time.time() - started) * 1000 > deadline_ms:
            raise TimeoutError("captcha deadline exceeded")

    if is_puzzle_captcha(page):
        _log("[Bind: CAPTCHA] FunCaptcha/puzzle unsupported")
        return False

    ready = wait_captcha_press_ready(page, wait_ms=min(60000, deadline_ms), log=_log)
    if not ready:
        _log("[Bind: CAPTCHA] press-hold not loaded")
        return False

    long_press_count = 0
    round_failed = True
    max_rounds = max(1, int(max_rounds))
    loop_limit = max_rounds + 1
    idle_ticks = 0

    _log(f"[Bind: CAPTCHA] start maxRounds={max_rounds} deadlineMs={deadline_ms}")

    while long_press_count < loop_limit:
        check_deadline()
        page.wait_for_timeout(200)
        frame1, frame2 = captcha_frame_locators(page)

        try:
            if page.get_by_text("\u4e00\u4e9b\u5f02\u5e38\u6d3b\u52a8").count() > 0:
                _log("[Bind: CAPTCHA] IP rate-limit text")
                return False
            if page.get_by_text("\u6b64\u7ad9\u70b9\u6b63\u5728\u7ef4\u62a4").count() > 0:
                _log("[Bind: CAPTCHA] site maintenance")
                return False
        except Exception:
            pass

        wait_hit = captcha_has_any_text(page, frame1, frame2, CAPTCHA_FIELD_TEXTS["wait"])
        if wait_hit:
            idle_ticks = 0
            continue

        if round_failed:
            if long_press_count >= max_rounds:
                _log(f"[Bind: CAPTCHA] maxRounds reached ({max_rounds})")
                break
            try:
                hit = captcha_get_box(page, frame1, frame2, CAPTCHA_PRESS_SELS)
                if not hit:
                    hold_t = captcha_has_any_text(
                        page, frame1, frame2, CAPTCHA_FIELD_TEXTS["holdHint"]
                    )
                    if hold_t:
                        try:
                            outer = page.locator(CAPTCHA_OUTER_IFRAME).first
                            ob = outer.bounding_box()
                            if ob and ob.get("width", 0) > 20 and ob.get("height", 0) > 20:
                                hit = {"box": ob, "via": "outer-iframe-fallback", "sel": hold_t}
                        except Exception:
                            pass
                if hit:
                    long_press_count += 1
                    round_failed = False
                    idle_ticks = 0
                    captcha_press_at(
                        page,
                        hit["box"],
                        10,
                        f"[Bind: CAPTCHA] long-press #{long_press_count}/{max_rounds} via={hit['via']}",
                        2000,
                        5000,
                        log=_log,
                    )
                    page.wait_for_timeout(1500)
                    continue
            except Exception as e:
                _log(f"[Bind: CAPTCHA] long-press err {e}")

        try:
            frame1, frame2 = captcha_frame_locators(page)
            hit = captcha_get_box(page, frame1, frame2, CAPTCHA_AGAIN_SELS)
            if hit:
                idle_ticks = 0
                captcha_press_at(
                    page,
                    hit["box"],
                    20,
                    f"[Bind: CAPTCHA] quick-tap via={hit['via']}",
                    80,
                    220,
                    log=_log,
                )
                page.wait_for_timeout(1200)
                continue
        except Exception as e:
            _log(f"[Bind: CAPTCHA] again-press err {e}")

        # Success signals
        try:
            if page.get_by_text("\u53d6\u6d88").count() > 0 and page.locator(CAPTCHA_OUTER_IFRAME).count() == 0:
                _log("[Bind: CAPTCHA] cleared (cancel visible, no outer)")
                return True
        except Exception:
            pass
        try:
            if page.locator(CAPTCHA_OUTER_IFRAME).count() == 0:
                # no challenge shell
                press_left = captcha_get_box(page, frame1, frame2, CAPTCHA_PRESS_SELS)
                if not press_left and not captcha_has_any_text(
                    page, frame1, frame2, CAPTCHA_FIELD_TEXTS["challenge"]
                ):
                    _log("[Bind: CAPTCHA] challenge gone")
                    return True
        except Exception:
            pass

        retry_hit = captcha_has_any_text(page, frame1, frame2, CAPTCHA_FIELD_TEXTS["retry"])
        if retry_hit:
            round_failed = True
            idle_ticks = 0
            _log(f"[Bind: CAPTCHA] retry prompt: {retry_hit}")
            page.wait_for_timeout(400)
            continue

        # still challenged?
        press_left = captcha_get_box(page, frame1, frame2, CAPTCHA_PRESS_SELS)
        if press_left:
            round_failed = True
            idle_ticks += 1
        else:
            idle_ticks += 1

        if idle_ticks > 40:
            _log("[Bind: CAPTCHA] idle timeout in loop")
            break
        page.wait_for_timeout(300)

    # final check
    try:
        frame1, frame2 = captcha_frame_locators(page)
        still = captcha_get_box(page, frame1, frame2, CAPTCHA_PRESS_SELS)
        if not still and page.locator(CAPTCHA_OUTER_IFRAME).count() == 0:
            return True
    except Exception:
        pass
    return False


def solve_captcha_or_raise(page, ctx: dict | None = None, *, log: LogFn | None = None) -> bool:
    """Adapter for actions.handle_interrupt captcha_solver callback.

    Raises CaptchaSkipError on failure so runner can map to status=captcha.
    """
    from .actions import CaptchaSkipError

    cfg = (ctx or {}).get("config")
    max_rounds = 3
    deadline_ms = 120_000
    if cfg is not None:
        max_rounds = getattr(cfg, "captcha_max_rounds", 3)
        deadline_ms = getattr(cfg, "captcha_deadline_ms", 120_000)

    if is_puzzle_captcha(page):
        raise CaptchaSkipError("CAPTCHA_SKIP puzzle/FunCaptcha")

    ok = handle_press_hold_captcha(
        page, max_rounds=max_rounds, deadline_ms=deadline_ms, log=log
    )
    if not ok:
        raise CaptchaSkipError("CAPTCHA_SKIP press-hold failed")
    return True


__all__ = [
    "handle_press_hold_captcha",
    "solve_captcha_or_raise",
    "is_puzzle_captcha",
    "wait_captcha_press_ready",
]
