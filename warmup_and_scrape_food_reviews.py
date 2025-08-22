# warmup_then_scrape_same_context.py
import re, sys, time, json, csv, os, argparse, random
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

#_bubble_re = re.compile(r"(\d+(?:\.\d+)?)\s*bubbles", re.I)

def extract_rating(label: str | None) -> float | None:
    if not label:
        return None
    # "4 of 5 bubbles", "4.0 of 5 bubbles"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:of|out of)\s*5\s*bubbles", label, re.I)
    if not m:
        # fallback: "4 bubbles"
        m = re.search(r"(\d+(?:\.\d+)?)\s*bubbles", label, re.I)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def pick_longest_text(loc):
    try:
        n = loc.count()
        best = ""
        for i in range(min(n, 40)):
            try:
                t = (loc.nth(i).inner_text() or "").strip()
                if len(t) > len(best): best = t
            except: pass
        return best or None
    except: return None

def rsleep(a=0.6, b=1.1):
    time.sleep(random.uniform(a, b))

def human_scroll(page, steps=10):
    for _ in range(steps):
        page.mouse.wheel(0, 800)           # mimic user scroll
        # don't wait for 'networkidle' (can hang on TA)
        page.wait_for_timeout(300)          # ~0.3s pause

def looks_like_verification(page) -> bool:
    try:
        txt = (page.inner_text("body") or "").lower()
    except: 
        return False
    if "verification required" in txt or "slide right to complete the puzzle" in txt:
        return True
    # DataDome iframe
    try:
        if page.locator("iframe[src*='captcha-delivery.com'], iframe[title*='captcha']").count():
            return True
    except: pass
    return False

from urllib.parse import urljoin

def dismiss_overlays(page):
    """Try to dismiss Braze/Appboy overlays that block clicks."""
    try:
        # Hit ESC once (sometimes closes modals)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass

    try:
        # Remove common Braze/Appboy containers
        page.evaluate(
            """
            () => {
                // div roots
                document.querySelectorAll('.ab-iam-root, .ab-iam-root-v3, div[id*="braze"], div[id*="appboy"]')
                    .forEach(el => el.remove());

                // iframes
                document.querySelectorAll('iframe[src*="braze"], iframe[src*="appboy"], iframe[class*="ab-iam-root"]')
                    .forEach(el => el.remove());
            }
            """
        )
        page.wait_for_timeout(200)
    except Exception:
        pass


def ensure_on_reviews(page):
    # 0) clear/dismiss anything that could block clicks
    try:
        dismiss_overlays(page)   # your helper that ESC + removes .ab-iam-root / iframes
    except Exception:
        pass

    # 1) fast path: if #REVIEWS exists, jump to it directly
    try:
        if page.locator("#REVIEWS").count():
            page.evaluate("() => { location.hash = 'REVIEWS'; }")
            page.locator("#REVIEWS").scroll_into_view_if_needed()
            page.wait_for_timeout(250)
    except Exception:
        pass

    selectors = [
        "a[data-automation='seeAllReviews']",
        "[data-test-target='reviews-tab']",
        "a[href*='#REVIEWS']",
        "a[href*='-Reviews-']",
        "a[aria-controls*='REVIEWS']",
        "a[href*='Reviews-'][role='tab']",
    ]

    # 2) try to click one of the entry points
    for sel in selectors:
        el = page.locator(sel).first
        if not el.count():
            continue

        # make sure it’s visible and nothing is covering it
        try:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(120)
            dismiss_overlays(page)
        except Exception:
            pass

        # A) normal click
        try:
            el.click(timeout=1200)
        except Exception:
            # B) JS click (bypasses some pointer-event traps)
            try:
                page.evaluate("(e)=>e.click()", el)
            except Exception:
                # C) navigate via href as a last resort
                try:
                    href = (el.get_attribute("href") or "").strip()
                    if href:
                        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
                except Exception:
                    pass

        # done? (reviews list present)
        if page.locator("div[data-test-target='review-card'], [data-automation='reviewCard']").count():
            return

    # 3) final fallback: force the hash and scroll
    try:
        page.evaluate("() => { location.hash = 'REVIEWS'; }")
        page.wait_for_timeout(200)
        page.locator("#REVIEWS").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
    except Exception:
        pass


def parse_current_page(page, debug_dir: Optional[Path], page_idx: int, target: str) -> List[Dict[str, Any]]:
    """解析目前頁面的所有評論卡。"""
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        snap = debug_dir / f"page_{page_idx:03d}.png"
        try:
            page.screenshot(path=str(snap), full_page=True)
            print(f"[DEBUG] Saved screenshot: {snap}")
        except Exception as e:
            print(f"[WARN] screenshot failed: {e}")

    # 展開 Read more
    try:
        expanders = page.locator("text=/^(Read more|More|Show more|更多|もっと読む)$/i")
        for i in range(min(expanders.count(), 20)):
            try:
                expanders.nth(i).click(timeout=800)
                rsleep(0.2, 0.45)
            except: pass
    except: pass

    # 支援 reviewText 作為卡片選擇器
    cards_sel = "div[data-test-target='review-card'], [data-automation='reviewCard'], " \
                "div[data-test-target='HR_CC_CARD'], div[data-test-target='reviewText'], " \
                "[data-automation='reviewText'], span[data-automation^='reviewText_'], " \
                "div[class*='JVaPo']"
    cards = page.locator(cards_sel)
    count = cards.count()
    print(f"[INFO] Found {count} review cards on page {page_idx}")
    out: List[Dict[str, Any]] = []

    for i in range(count):
        card = cards.nth(i)
        # review title
        title = None

        # 1) New TA markup seen in your screenshot
        loc = card.locator("a[href*='ShowUserReviews'], span.yCeTE").first
        if loc.count():
            title = (loc.text_content() or "").strip()
            print(f"[DEBUG] Found title in new markup: {title!r}")


        # 2) Foods 方案：直接抓 <a> 本身的文字（像你 screenshot 看到的 Beautiful setting!）
        if not title:
            loc2 = card.locator("a[href*='ShowUserReviews']").first
            if loc2.count():
                title = (loc2.text_content() or "").strip()
                print(f"[DEBUG] Found title in <a>: {title!r}")
                
        # 2) Fallbacks (older markups)
        if not title:
            for sel in [
                "[data-automation='reviewTitle']",
                "a[data-test-target='review-title']",
                "span[data-test-target='review-title']",
                "h3, h4",
            ]:
                l = card.locator(sel)
                if l.count():
                    try:
                        title = (l.first.text_content() or "").strip()
                        if title:
                            print(f"[DEBUG] Found title in fallback markup: {title!r}")
                            break
                    except Exception:
                        pass

        # text  —— 專抓評論內容容器，避免落到 "Written ..." 或 disclaimer
        def _clean_review_text(s: str | None) -> str | None:
            if not s:
                return None
            s = s.strip()
            # 去掉常見尾巴
            s = re.sub(r"\b(Read more|Show less)\b.*$", "", s, flags=re.I)
            s = re.sub(r"^Written\s+\w+\s+\d{1,2},\s+\d{4}.*$", "", s, flags=re.I)
            s = re.sub(r"This review is the subjective opinion.*$", "", s, flags=re.I)
            s = re.sub(r"\s+", " ", s).strip()
            return s or None

        text = None

        # 1) 目前版型：div.bgMZj（外層容器）與其內的 span.jguWG / span.yCeTE
        #    注意：bgMZj 是雜湊 class，改用 [class*='bgMZj'] 做模糊匹配以提高容錯
        cand_sel = (
            ":scope div[class*='bgMZj'], "         # 內文容器
            ":scope div[class*='bgMZj'] span, "    # 容器內的文字節點
            ":scope span.jguWG, "
            ":scope span.yCeTE, "
            ":scope [data-automation='reviewText'], "
            ":scope [data-test-target='review-text']"
        )

        # ☆☆☆ 新增：定義「回覆」區塊（各種可能）
        responses_sel = (
            ":scope [data-automation*='Response'], "          # e.g. managementResponse / ownerResponse
            ":scope [data-test-target*='response'], "         # 有些頁面會用 data-test-target
            ":scope :text('Response from'), "                 # 英文 UI
            ":scope :text('Management response'), "
            ":scope :text('Owner response')"
        )
        responses = card.locator(responses_sel)

        # ☆☆☆ 新增：把位於回覆區塊內的節點排除
        cands = card.locator(cand_sel).filter(has_not=responses)

        if cands.count():
            pieces = []
            for k in range(min(cands.count(), 30)):
                try:
                    node = cands.nth(k)
                    t = (node.text_content() or "").strip()
                    t = _clean_review_text(t)
                    # ☆☆☆ 再加一層保護：明顯回覆語氣就略過
                    if t and re.match(r"^\s*(dear|親愛|尊敬|您好)\b", t, re.I):
                        continue
                    # 既有：略過 "Written ..."
                    if t and not t.lower().startswith("written "):
                        pieces.append(t)
                except:
                    pass
            if pieces:
                text = max(pieces, key=len)


        # 2) 萬一上面沒抓到，再用語系/通用區塊兜底
        if not text:
            loc = card.locator(":scope span[lang]")
            t = _clean_review_text((loc.first.text_content() or "") if loc.count() else "")
            if t:
                text = t

        if not text:
            loc = card.locator(":scope p, :scope q, :scope div")
            t = _clean_review_text((loc.first.text_content() or "") if loc.count() else "")
            if t:
                text = t



        # rating (supports SVG <title> and aria-labelledby)
        rating = None
        label = ""

        # A) aria-label directly on any element
        rate_el = card.locator("[aria-label*='bubbles']").first
        if rate_el.count():
            label = (rate_el.get_attribute("aria-label") or "").strip()

        # B) TripAdvisor’s SVG with <title> and/or aria-labelledby
        if not label:
            svg = card.locator("svg[data-automation='bubbleRatingImage']").first
            if svg.count():
                # try <title> text (NOT inner_text on non-HTMLElement)
                ttl = svg.locator("title")
                if ttl.count():
                    label = (ttl.first.text_content() or "").strip()

                # try aria-labelledby (can contain multiple ids)
                if not label:
                    ref = (svg.get_attribute("aria-labelledby") or "").strip()
                    if ref:
                        for rid in ref.split():
                            # look inside card first, then globally as fallback
                            ref_loc = card.locator(f"#{rid}")
                            if not ref_loc.count():
                                ref_loc = page.locator(f"#{rid}")
                            if ref_loc.count():
                                label = (ref_loc.first.text_content() or "").strip()
                                if label:
                                    break

                # final fallback: aria-label on the svg itself
                if not label:
                    label = (svg.get_attribute("aria-label") or "").strip()

        # C) last resort: any svg/span with aria-label
        if not label:
            alt = card.locator("svg[aria-label*='bubbles'], span[aria-label*='bubbles']").first
            if alt.count():
                label = (alt.get_attribute("aria-label") or "").strip()

        rating = extract_rating(label)
        # Optional debug:
        # if rating is None and label: print(f"[DEBUG] Unparsed rating label: {label!r}")


        # dates
        written_date = None; travel_date = None
        try:
            wd = card.locator("span:has-text('Written')")
            if wd.count(): written_date = (wd.first.inner_text() or "").strip()
        except: pass
        try:
            exp = card.locator(":scope :text('Date of experience')")
            if exp.count():
                travel_date = (exp.first.inner_text() or "").strip()
            else:
                blob = card.inner_text()
                m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}", blob, re.I)
                if m: travel_date = m.group(0)
        except: pass

        # language
        language = None
        try:
            lang_spans = card.locator("span[lang]")
            if lang_spans.count(): language = lang_spans.first.get_attribute("lang")
        except: pass

        # author & location
        author = None
        location_txt = None

        # ---- author ----
        try:
            # 正式 selector（有時存在）
            name_loc = card.locator("[data-automation='memberName'], a[data-automation='reviewer-name']").first
            if name_loc.count():
                author = (name_loc.text_content() or "").strip() or None
            else:
                # 最後備援：卡片裡的第一個 link 當名字
                first_link = card.get_by_role("link").first
                if first_link.count():
                    author = (first_link.text_content() or "").strip() or None
        except Exception:
            pass

        # ---- location ----
        try:
            # 1) 舊版/部分頁面
            loc_loc = card.locator("[data-automation='reviewerLocation'], span[data-test-target='reviewer-location']").first
            if loc_loc.count():
                location_txt = (loc_loc.text_content() or "").strip()

            # 2) 新版：class 含 navcl 的容器
            if not location_txt:
                nav = card.locator(":scope div[class*='navcl']").first
                if nav.count():
                    # 位置通常是第一個 span；排除「貢獻數」的小徽章（常見 class IugUm）
                    sp = nav.locator("span:not(.IugUm):not([class*='IugUm'])").first
                    if not sp.count():
                        sp = nav.locator("span").first
                    t = (sp.text_content() or "").strip()
                    # 濾掉非地點的字樣（保守）
                    if t and not re.search(r"\b(contribution|review)\b", t, re.I):
                        location_txt = t

            # 3) 兜底：找像 "City, Country" 這種有逗號的 span
            if not location_txt:
                maybe = card.locator(":scope span:has-text(',')").first
                if maybe.count():
                    t = (maybe.text_content() or "").strip()
                    if t and len(t.split()) <= 5:  # 避免抓到整段內文
                        location_txt = t
        except Exception:
            pass


        # contribution/helpful
        contribution_count = None; helpful_votes = None
        try:
            contrib = card.locator(":scope span:has-text('contribution')")
            if contrib.count():
                m = re.search(r"(\d+)", contrib.first.inner_text() or "")
                if m: contribution_count = int(m.group(1))
        except: pass
        try:
            helpful = card.locator(":scope span:has-text('helpful')")
            if helpful.count():
                m = re.search(r"(\d+)", helpful.first.inner_text() or "")
                if m: helpful_votes = int(m.group(1))
        except: pass

        out.append({
            "title": title, "text": text, "rating": rating,
            "travel_date": travel_date, "written_date": written_date,
            "language": language, "author": author, "location": location_txt,
            "contribution_count": contribution_count, "helpful_votes": helpful_votes,
            "url": target
        })
    return out

def first_card_key(page) -> str | None:
    """回傳目前頁面第一張評論卡的 key（title/author/written_date 合併）"""
    cards = page.locator("div[data-test-target='review-card'], [data-automation='reviewCard'], div[data-test-target='HR_CC_CARD']")
    if not cards.count():
        return None
    card = cards.first

    try:
        # 標題
        title = ""
        tloc = card.locator("a[href*='ShowUserReviews'] span, span.yCeTE, [data-automation='reviewTitle'], a[data-test-target='review-title'], span[data-test-target='review-title'], h3, h4")
        if tloc.count():
            title = (tloc.first.text_content() or "").strip()
    except Exception:
        title = ""

    try:
        # 作者
        author = ""
        aloc = card.locator("[data-automation='memberName']")
        if aloc.count():
            author = (aloc.first.text_content() or "").strip()
    except Exception:
        author = ""

    try:
        # Written 日期
        wdate = ""
        wd = card.locator("span:has-text('Written')")
        if wd.count():
            wdate = (wd.first.text_content() or "").strip()
    except Exception:
        wdate = ""

    key = "|".join([title, author, wdate]).strip()
    return key or None

def no_more_next(page) -> bool:
    """UI 層面看起來已沒有下一頁可點"""
    candidates = [
        "[data-smoke-attr='pagination-next-arrow']",
        "a[aria-label='Next page']",
        "a[aria-label*='Next page']",
        "a[aria-label='Next']",
        "a[aria-label*='Next']",
        "button[aria-label*='Next']",
        "li[title*='Next Page'] a",
        "nav[aria-label='Pagination'] a:has-text('Next')",
        "a[rel='next']",
        "a[data-page-number][aria-label*='Next']",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if not loc.count():
            continue

        el = loc.first
        try:
            # 不可見就當作沒有 next
            if not el.is_visible():
                continue
        except Exception:
            continue

        try:
            # 明顯禁用或隱藏
            aria_hidden = (el.get_attribute("aria-hidden") or "").lower()
            aria_disabled = (el.get_attribute("aria-disabled") or "").lower()
            disabled = el.get_attribute("disabled")
            tabindex = el.get_attribute("tabindex")

            if aria_hidden == "true" or aria_disabled == "true" or disabled is not None or tabindex == "-1":
                continue

            # 父層是否 disabled/li.disabled
            parent_disabled = el.evaluate("""
                (n) => {
                    const p = n.closest('li[disabled],button[disabled]');
                    return !!p;
                }
            """)
            if parent_disabled:
                continue
        except Exception:
            pass

        # 找到一個可見且看起來可點的 next => 還有下一頁
        return False

    # 沒有任何候選，或都不可點
    return True


def click_next_page(page) -> bool:
    sels = [
        "[data-smoke-attr='pagination-next-arrow']",
        "a[aria-label='Next page']",
        "a[aria-label*='Next page']",
        "a[aria-label='Next']",
        "a[aria-label*='Next']",
        "button[aria-label*='Next']",
        "li[title*='Next Page'] a",
        "nav[aria-label='Pagination'] a:has-text('Next')",
        "a[rel='next']",
        "a[data-page-number][aria-label*='Next']",
    ]

    # 點擊前的狀態
    before_key = first_card_key(page)
    before_url = page.url

    for sel in sels:
        loc = page.locator(sel).first
        if not loc.count():
            continue
        try:
            loc.scroll_into_view_if_needed()
            page.wait_for_timeout(120)
        except Exception:
            pass

        try:
            # 溫和關閉遮罩
            try:
                dismiss_overlays(page)
            except Exception:
                pass

            # 嘗試帶 navigation… 有些頁面只局部更新，會 fallback
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=6000):
                    loc.click()
            except Exception:
                loc.click(timeout=2500, force=True)

            page.wait_for_timeout(600)
            if looks_like_verification(page):
                print("\n[ACTION] 翻頁後遇到驗證，請完成驗證再按 Enter。")
                input(">> 按 Enter 繼續… ")

            # URL 沒變就用 href 強制跳
            if page.url == before_url:
                href = (loc.get_attribute("href") or "").strip()
                if href:
                    page.goto(urljoin(before_url, href), wait_until="domcontentloaded")
                    page.wait_for_timeout(500)

            # 檢查內容是否真的改變
            after_key = first_card_key(page)
            if (after_key and after_key != before_key) or (page.url != before_url):
                return True

        except Exception as e:
            print(f"[WARN] next click failed via {sel}: {e}")
            continue

    return False

def run_same_context(target: str, max_pages: int, timeout_ms: int, debug_dir: Optional[Path], out_json: Optional[Path], out_csv: Optional[Path], page):
    # with sync_playwright() as p:
    #     # 1) 開 headed 讓你手動過驗證
    #     browser = p.chromium.launch(headless=False, args=[
    #         "--disable-blink-features=AutomationControlled"
    #     ])
    #     ctx = browser.new_context(
    #         user_agent=UA,
    #         locale="en-US",
    #         viewport={"width": 1366, "height": 900},
    #         extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    #     )
    #     ctx.add_init_script("""
    #         Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    #         window.chrome = { runtime: {} };
    #         Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    #         Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    #     """)
    #     page = ctx.new_page()
    #     page.set_default_timeout(timeout_ms)

        print(f"[INFO] goto: {target}")
        #page.goto(target, wait_until="domcontentloaded")
        page.goto(target, wait_until="load", timeout=60000)
        rsleep()

        # 自動點 cookie
        try:
            btn = page.get_by_role("button", name=re.compile(r"(Accept|Agree|I agree|OK)", re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(); rsleep()
        except: pass

        # 若是驗證頁，請在視窗中手動完成
        if looks_like_verification(page):
            print("\n[ACTION] 視窗顯示驗證頁，請手動完成驗證。完成後請回到這個終端機按 Enter 繼續。")
            input(">> 按 Enter 繼續… ")

        # 確認在 Reviews 列表
        ensure_on_reviews(page)

        # 防止 Lazyload：先滾幾次
        for _ in range(5):
            human_scroll(page, steps=2)

        # 取得景點名稱
        attraction = None
        try:
            h1 = page.locator("h1[data-test-target='mainH1']")
            if h1.count():
                main_span = h1.first.locator("span").first
                if main_span.count():
                    attraction = (main_span.text_content() or "").strip()
                else:
                    raw = h1.first.inner_text() or ""
                    attraction = raw.split("Unclaimed")[0].strip()
            else:
                h1 = page.locator("h1")
                if h1.count():
                    raw = h1.first.inner_text() or ""
                    attraction = raw.split("Unclaimed")[0].strip()
        except Exception as e:
            print(f"[WARN] 景點名稱擷取失敗: {e}")
        if not attraction:
            attraction = "(unknown)"

        # 2) 同一個 page 直接開始爬
        all_reviews: List[Dict[str, Any]] = []
        page_index = 1
        visited = set()
        same_count = 0    # 連續沒變化次數

        while page_index <= max_pages:
            print(f"[INFO] parsing page {page_index} | url={page.url}")
            visited.add(page.url)

            try:
                page.wait_for_selector("div[data-test-target='review-card'], [data-automation='reviewCard'], div[data-test-target='review-text'], [data-automation='reviewText']", timeout=8000)
            except PWTimeoutError:
                print("[WARN] reviewCard not found yet; try scroll more")
                human_scroll(page, steps=3)

            reviews = parse_current_page(page, debug_dir, page_index, target)
            # 將景點名稱加到每筆 review
            for r in reviews:
                r["attraction"] = attraction
            all_reviews.extend(reviews)

            # —— 停止條件 1：UI 已看不到下一頁
            if no_more_next(page):
                print("[INFO] No more next page (disabled / hidden). Stop.")
                break

            # —— 嘗試翻頁
            before_key = first_card_key(page)
            moved = click_next_page(page)

            if not moved:
                print("[INFO] Next click not effective, stop.")
                break

            # —— 停止條件 2：內容沒有變化（保守：連續兩次）
            after_key = first_card_key(page)
            if before_key and after_key == before_key:
                same_count += 1
            else:
                same_count = 0
            if same_count >= 2:
                print("[INFO] Page content not changing across next attempts. Stop.")
                break

            # 也可避免 URL 迴圈
            if page.url in visited:
                print("[INFO] URL repeated. Stop.")
                break

            page_index += 1

        # 輸出
        if out_json:
            out_json.parent.mkdir(parents=True, exist_ok=True)
            # attraction 欄位放第一
            def move_attraction_first(d):
                if "attraction" in d:
                    keys = ["attraction"] + [k for k in d if k != "attraction"]
                    return {k: d.get(k) for k in keys}
                return d
            out_json.write_text(json.dumps([move_attraction_first(r) for r in all_reviews], ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[DONE] Saved JSON: {out_json} ({len(all_reviews)} rows)")
        if out_csv:
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                fieldnames = ["attraction"] + [k for k in all_reviews[0].keys() if k != "attraction"] if all_reviews else ["attraction","title","text","rating","travel_date","written_date","language","author","location","contribution_count","helpful_votes","url"]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for r in all_reviews: w.writerow(r)
            print(f"[DONE] Saved CSV:  {out_csv} ({len(all_reviews)} rows)")

        
        return all_reviews

def cli():
    import json

    base_url = ""
    json_path = Path("./TripAdv_Foods_List.json").resolve()

    # 載入 JSON
    with open(json_path, "r", encoding="utf-8") as f:
        urls = json.load(f)

    if not isinstance(urls, list):
        print(f"[ERROR] JSON 應該是 list，但得到 {type(urls)}")
        sys.exit(1)

    debug_dir = Path("debug_shots_food").resolve()
    out_json  = Path("reviews_food.json").resolve()
    out_csv   = Path("reviews_food.csv").resolve()

    print(f"[INFO] CWD={os.getcwd()}")
    print(f"[INFO] debug_dir={debug_dir}")
    print(f"[INFO] out_json={out_json}")
    print(f"[INFO] out_csv={out_csv}")

    processed_path = Path("processed_urls_food.txt").resolve()
    # 讀取已處理過的 URL
    processed = set()
    if processed_path.exists():
        with open(processed_path, "r", encoding="utf-8") as pf:
            for line in pf:
                processed.add(line.strip())

    # 檢查 CSV 是否存在
    csv_exists = out_csv.exists()
    if not csv_exists:
        # 新建空白 CSV
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            fieldnames = ["attraction","title","text","rating","travel_date","written_date","language","author","location","contribution_count","helpful_votes","url"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()

    with sync_playwright() as p:
        # 1) 開 headed 讓你手動過驗證
        browser = p.chromium.launch(headless=False, args=[
            "--disable-blink-features=AutomationControlled"
        ])
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # block Braze/Appboy
        # ctx.route("**/*appboy*/*", lambda r: r.abort())
        # ctx.route("**/*braze*/*",   lambda r: r.abort())
        # ctx.route("**/js.appboycdn.com/*", lambda r: r.abort())
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)
        page = ctx.new_page()
        timeout_ms = 15000
        page.set_default_timeout(timeout_ms)

        for rel_path in urls:
            full_url = base_url + rel_path
            
            if full_url in processed or "#REVIEWS" in full_url:
                print(f"[SKIP] 已處理過: {full_url}")
                continue
            print(f"[INFO] 處理 URL: {full_url}")
            
            reviews = run_same_context(
                target=full_url,
                max_pages=300,
                timeout_ms=15000,
                debug_dir=debug_dir,
                out_json=None,  # 不每次都寫 json
                out_csv=None,   # 不每次都寫 csv
                page=page
            )
            # 追加到 CSV
            if reviews:
                with out_csv.open("a", newline="", encoding="utf-8") as f:
                    fieldnames = ["attraction"] + [k for k in reviews[0].keys() if k != "attraction"]
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    for r in reviews:
                        w.writerow(r)
            # 記錄已處理 URL
            with open(processed_path, "a", encoding="utf-8") as pf:
                pf.write(full_url + "\n")
            
        browser.close()
    print(f"[DONE] 所有 URL 處理完畢。結果已追加到 {out_csv}")


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(1)
