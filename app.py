# app.py
import re
import time
import random
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

import sys, logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],  # 明確丟到 STDOUT
    force=True,  # 若其他地方已設定過 logging，強制覆蓋
)


app = Flask(__name__)

# ---------- Small helpers ----------

def _rand_sleep(a=0.6, b=1.1):
    time.sleep(random.uniform(a, b))

@dataclass
class Review:
    title: Optional[str]
    text: Optional[str]
    rating: Optional[float]
    travel_date: Optional[str]
    written_date: Optional[str]
    language: Optional[str]
    author: Optional[str]
    location: Optional[str]
    contribution_count: Optional[int]
    helpful_votes: Optional[int]
    url: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# Parse "... bubbles" into float rating
_bubble_re = re.compile(r"(\d+(?:\.\d+)?)\s*bubbles", re.I)

def extract_rating(label: Optional[str]) -> Optional[float]:
    if not label:
        return None
    m = _bubble_re.search(label)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    # Fallback: "5 of 5 bubbles"
    if "of 5" in label:
        try:
            return float(label.split("of 5")[0].strip())
        except Exception:
            return None
    return None

def pick_longest_text(loc) -> Optional[str]:
    """
    從 locator 裡挑最長的可見文本，排除版權聲明等無效內容。
    """
    try:
        n = loc.count()
    except Exception:
        return None
    texts: List[str] = []
    for j in range(min(n, 16)):
        try:
            t = loc.nth(j).inner_text().strip()
            if not t:
                continue
            if "This review is the subjective opinion" in t:
                continue
            texts.append(t)
        except Exception:
            pass
    return max(texts, key=len) if texts else None

def _human_scroll(page, steps=10):
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight*0.6));")
        page.wait_for_load_state("networkidle")
        _rand_sleep(0.25, 0.55)


def _looks_like_verification(page) -> bool:
    txt = (page.inner_text("body") or "").lower()
    if "verification required" in txt or "slide right to complete the puzzle" in txt:
        return True
    # puzzle widgets sometimes expose role/button with speaker or camera icons
    try:
        if page.locator("text=/Verification Required/i").count(): return True
        if page.locator("iframe[src*='arkoselabs'], div[aria-label*='captcha']").count(): return True
    except Exception:
        pass
    return False

# ---------- Core scraping ----------
def scrape_tripadvisor_reviews(
    url: str,
    max_pages: int = 50,
    page_timeout_ms: int = 15000,
    storage_state: Optional[str] = None,
) -> List[Dict[str, Any]]:

    reviews: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ])

        context_kwargs = dict(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        if storage_state and os.path.exists(storage_state):
            logging.info(f"Loading storage state from: {storage_state}")
            context_kwargs["storage_state"] = storage_state

        context = browser.new_context(**context_kwargs)

        # mask common automation fingerprints
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        page = context.new_page()
        page.set_default_timeout(page_timeout_ms)

        # --- navigate ---
        page.goto(url, wait_until="domcontentloaded")
        _rand_sleep()

        # quick CAPTCHA gate check
        if _looks_like_verification(page):
            # dump html to inspect
            try:
                with open("/data/shared/ta_captcha.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception:
                pass
            raise RuntimeError("Tripadvisor verification page encountered (CAPTCHA).")

        # Cookie/consent
        try:
            consent = page.get_by_role("button", name=re.compile(r"(Accept|Agree|I agree|OK)", re.I))
            if consent.count() > 0 and consent.first.is_visible():
                consent.first.click()
                logging.info("Accepted cookie.")
                _rand_sleep()
        except Exception:
            logging.info("No cookie banner or click failed; continue.")

        # ensure we're on the reviews list view
        try:
            # try several stable entry points
            candidates = [
                "a[data-automation='seeAllReviews']",
                "[data-test-target='reviews-tab']",
                "a[href*='#REVIEWS']",
                "a[href*='-Reviews-']",
                "a[aria-controls*='REVIEWS']",
            ]
            for sel in candidates:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    logging.info(f"Clicking reviews entry: {sel}")
                    loc.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    _rand_sleep()
                    break
        except Exception:
            pass

        # If redirected to verification after clicking
        if _looks_like_verification(page):
            try:
                with open("/data/shared/ta_captcha.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception:
                pass
            raise RuntimeError("Tripadvisor verification page encountered (CAPTCHA).")

        # Turn off auto-translate if present
        try:
            show_original = page.get_by_role("button", name=re.compile(r"(Show original reviews|Show original)", re.I))
            if show_original.count() > 0 and show_original.first.is_visible():
                show_original.first.click()
                _rand_sleep()
        except Exception:
            pass

        def dump_html(tag="init"):
            try:
                html = page.content()
                with open(f"/data/shared/ta_{tag}.html","w",encoding="utf-8") as f: f.write(html)
            except Exception:
                pass

        # wait for review cards (trigger lazyload via human scroll)
        review_sel = "div[data-test-target='review-card'], [data-automation='reviewCard']"
        found = False
        for _ in range(12):
            if page.locator(review_sel).count() > 0:
                found = True
                break
            _human_scroll(page, steps=2)
        if not found:
            try:
                page.wait_for_selector(review_sel, timeout=8000)
                found = True
            except PWTimeoutError:
                logging.info("reviewCard not found, dumping HTML for debug")
                dump_html("no_reviews")
                return []

        logging.info(f"Found {page.locator(review_sel).count()} review cards initially")

        # main pagination loop
        visited_page_urls = set()
        page_index = 1

        while page_index <= max_pages:
            visited_page_urls.add(page.url)

            # Expand "Read more"
            try:
                expanders = page.locator("text=/^(Read more|More|Show more|更多|もっと読む)$/i")
                cnt = expanders.count()
                for i in range(min(cnt, 20)):
                    try:
                        expanders.nth(i).click(timeout=1000)
                        _rand_sleep(0.2, 0.5)
                    except Exception:
                        pass
            except Exception:
                pass

            # parse cards
            card_selectors = [
                "[data-automation='reviewCard']",
                "div[data-test-target='review-card']",
                "div[data-test-target='HR_CC_CARD']",
            ]
            cards = page.locator(", ".join(card_selectors))
            count = cards.count()

            for i in range(count):
                card = cards.nth(i)

                # title
                title = None
                for sel in [
                    "[data-automation='reviewTitle']",
                    "a[data-test-target='review-title']",
                    "span[data-test-target='review-title']",
                    "h3, h4",
                ]:
                    loc = card.locator(sel)
                    if loc.count():
                        try:
                            title = (loc.first.inner_text() or "").strip()
                            if title:
                                break
                        except Exception:
                            pass

                # text
                text = None
                loc = card.locator("[data-automation='reviewText']")
                if loc.count():
                    text = pick_longest_text(loc)
                if not text:
                    loc = card.locator(":scope span[lang]")
                    text = pick_longest_text(loc)
                if not text:
                    loc = card.locator(":scope p, :scope q, :scope div")
                    text = pick_longest_text(loc)

                # rating
                rating = None
                try:
                    rate_el = card.locator("[aria-label*='bubbles']").first
                    if rate_el.count() == 0:
                        rate_el = card.locator("svg[aria-label*='bubbles'], span[aria-label*='bubbles']").first
                    if rate_el and rate_el.count():
                        label = rate_el.get_attribute("aria-label") or ""
                        rating = extract_rating(label)
                except Exception:
                    pass

                # dates
                written_date = None
                travel_date = None
                try:
                    wd = card.locator("span:has-text('Written')")
                    if wd.count():
                        written_date = (wd.first.inner_text() or "").strip()
                except Exception:
                    pass
                try:
                    exp = card.locator(":scope :text('Date of experience')")
                    if exp.count():
                        travel_date = (exp.first.inner_text() or "").strip()
                    else:
                        blob = card.inner_text()
                        m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}", blob, re.I)
                        if m: travel_date = m.group(0)
                except Exception:
                    pass

                # language
                language = None
                try:
                    lang_spans = card.locator("span[lang]")
                    if lang_spans.count():
                        language = lang_spans.first.get_attribute("lang")
                except Exception:
                    pass

                # author/location
                author = None
                location_txt = None
                try:
                    name_loc = card.locator("[data-automation='memberName']")
                    if name_loc.count():
                        author = (name_loc.first.inner_text() or "").strip() or None
                    else:
                        links = card.get_by_role("link")
                        if links.count():
                            author = (links.first.inner_text() or "").strip() or None
                except Exception:
                    pass
                try:
                    loc_loc = card.locator("[data-automation='reviewerLocation'], span[data-test-target='reviewer-location']")
                    if loc_loc.count():
                        location_txt = (loc_loc.first.inner_text() or "").strip()
                except Exception:
                    pass

                # contribution/helpful
                contribution_count = None
                helpful_votes = None
                try:
                    contrib = card.locator(":scope span:has-text('contribution')")
                    if contrib.count():
                        m = re.search(r"(\d+)", contrib.first.inner_text() or "")
                        if m: contribution_count = int(m.group(1))
                except Exception:
                    pass
                try:
                    helpful = card.locator(":scope span:has-text('helpful')")
                    if helpful.count():
                        m = re.search(r"(\d+)", helpful.first.inner_text() or "")
                        if m: helpful_votes = int(m.group(1))
                except Exception:
                    pass

                reviews.append({
                    "title": title,
                    "text": text,
                    "rating": rating,
                    "travel_date": travel_date,
                    "written_date": written_date,
                    "language": language,
                    "author": author,
                    "location": location_txt,
                    "contribution_count": contribution_count,
                    "helpful_votes": helpful_votes,
                    "url": page.url,
                })

            # go next page
            next_clicked = False
            next_selectors = [
                "nav[aria-label='Pagination'] a[aria-label*='Next']",
                "a[aria-label='Next page']",
                "a[aria-label='Next']",
                "button[aria-label='Next']",
                "li[title='Next Page'] a",
                "a[data-page-number][aria-label*='Next']",
            ]
            for sel in next_selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        el = loc.first
                        if el.is_enabled():
                            el.click()
                            _rand_sleep(1.0, 2.0)
                            page.wait_for_load_state("domcontentloaded")
                            if _looks_like_verification(page):
                                raise RuntimeError("CAPTCHA encountered on pagination.")
                            # loop guard
                            if page.url in visited_page_urls:
                                next_clicked = False
                            else:
                                next_clicked = True
                                break
                except Exception as e:
                    logging.info(f"Next-page click via {sel} failed: {e}")
                    pass

            if not next_clicked:
                break

            page_index += 1

        # persist session to re-use (reduces CAPTCHA later)
        try:
            context.storage_state(path=storage_state or "/data/shared/ta_state.json")
        except Exception:
            pass

        context.close()
        browser.close()

    return reviews


# ---------- Flask routes ----------

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/scrape")
def scrape():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    logging.info(f"Received scrape request for URL: {url}")
    if not url:
        return jsonify({"error": "Missing 'url' in JSON body"}), 400

    try:
        max_pages = int(data.get("max_pages", 50))
    except Exception:
        max_pages = 50
    try:
        timeout_ms = int(data.get("timeout_ms", 15000))
    except Exception:
        timeout_ms = 15000

    storage_state = data.get("storage_state")  # e.g. "/data/shared/ta_state.json"

    try:
        results = scrape_tripadvisor_reviews(
            url, max_pages=max_pages, page_timeout_ms=timeout_ms, storage_state=storage_state
        )
        return jsonify({"source": url, "count": len(results), "reviews": results})
    except RuntimeError as rexc:
        # explicit CAPTCHA detection bubbles up here
        return jsonify({"error": str(rexc), "type": "captcha"}), 403
    except PWTimeoutError as te:
        return jsonify({"error": f"Playwright timeout: {str(te)}"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- 新增：暖身用 helper ----
def warmup_tripadvisor(storage_state: str, target_url: str, headed: bool = True, timeout_ms: int = 30000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        page.goto(target_url, wait_until="domcontentloaded")
        time.sleep(1.0)

        # 嘗試按 Cookie 同意
        try:
            consent = page.get_by_role("button", name=re.compile(r"(Accept|Agree|I agree|OK)", re.I))
            if consent.count() > 0 and consent.first.is_visible():
                consent.first.click()
                time.sleep(0.8)
        except Exception:
            pass

        # 在 headed 模式下，若出現驗證頁，請手動解完，頁面會自動通過
        logging.info("If CAPTCHA appears, solve it manually (headed=True).")

        # 等待一下讓你解驗證（headed=True 才看得到）
        if headed:
            # 最多給 120 秒處理驗證
            for _ in range(120):
                body_txt = (page.inner_text("body") or "").lower()
                if "verification required" not in body_txt:
                    break
                time.sleep(1.0)

        # 存 session
        context.storage_state(path=storage_state)
        browser.close()
        return True

# ---- 新增：/warmup 路由 ----
@app.post("/warmup")
def warmup():
    """
    POST JSON:
    {
      "storage_state": "/data/shared/ta_state.json",  # 必填
      "target_url": "https://www.tripadvisor.com/",   # 選填，預設 TA 首頁
      "headed": true,                                  # 建議 true，便於手動解驗證
      "timeout_ms": 30000                              # 選填
    }
    """
    data = request.get_json(silent=True) or {}
    storage_state = data.get("storage_state") or "/data/shared/ta_state.json"
    target_url = data.get("target_url") or "https://www.tripadvisor.com/"
    headed = bool(data.get("headed", True))
    timeout_ms = int(data.get("timeout_ms", 30000))

    try:
        ok = warmup_tripadvisor(storage_state, target_url, headed=headed, timeout_ms=timeout_ms)
        return jsonify({"ok": ok, "storage_state": storage_state, "target_url": target_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # 讓 Docker/n8n 容器能連線
    app.run(host="0.0.0.0", port=5002)
