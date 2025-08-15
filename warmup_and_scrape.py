# warmup_then_scrape_same_context.py
import re, sys, time, json, csv, os, argparse, random
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

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

def ensure_on_reviews(page):
    selectors = [
        "a[data-automation='seeAllReviews']",
        "[data-test-target='reviews-tab']",
        "a[href*='#REVIEWS']",
        "a[href*='-Reviews-']",
        "a[aria-controls*='REVIEWS']",
        "a[href*='Reviews-'][role='tab']",
    ]
    for sel in selectors:
        el = page.locator(sel)
        if el.count() and el.first.is_visible():
            print(f"[INFO] Clicking reviews entry: {sel}")
            el.first.click()
            # Don't wait for navigation/loadstate; the URL may not change.
            break

    # Wait for reviews list to render instead
    try:
        page.wait_for_selector("div[data-test-target='review-card'], [data-automation='reviewCard']", timeout=8000)
    except:
        # help lazyload
        human_scroll(page, steps=6)

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

    cards_sel = "div[data-test-target='review-card'], [data-automation='reviewCard'], div[data-test-target='HR_CC_CARD']"
    cards = page.locator(cards_sel)
    count = cards.count()
    print(f"[INFO] Found {count} review cards on page {page_idx}")
    out: List[Dict[str, Any]] = []

    for i in range(count):
        card = cards.nth(i)
        # review title
        title = None

        # 1) New TA markup seen in your screenshot
        loc = card.locator("a[href*='ShowUserReviews'] span, span.yCeTE").first
        if loc.count():
            title = (loc.text_content() or "").strip()

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
        cands = card.locator(cand_sel)

        if cands.count():
            pieces = []
            for k in range(min(cands.count(), 30)):
                try:
                    t = (cands.nth(k).text_content() or "").strip()
                    t = _clean_review_text(t)
                    if t and not t.lower().startswith("written "):
                        pieces.append(t)
                except:
                    pass
            # 取最長的一段文字（最可能是完整內文）
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
        author = None; location_txt = None
        try:
            name_loc = card.locator("[data-automation='memberName']")
            if name_loc.count():
                author = (name_loc.first.inner_text() or "").strip() or None
            else:
                links = card.get_by_role("link")
                if links.count():
                    author = (links.first.inner_text() or "").strip() or None
        except: pass
        try:
            loc_loc = card.locator("[data-automation='reviewerLocation'], span[data-test-target='reviewer-location']")
            if loc_loc.count(): location_txt = (loc_loc.first.inner_text() or "").strip()
        except: pass

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

def run_same_context(target: str, max_pages: int, timeout_ms: int, debug_dir: Optional[Path], out_json: Optional[Path], out_csv: Optional[Path]):
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
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)
        page = ctx.new_page()
        page.set_default_timeout(timeout_ms)

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

        # 2) 同一個 page 直接開始爬
        all_reviews: List[Dict[str, Any]] = []
        page_idx = 1
        visited = set()

        while page_idx <= max_pages:
            print(f"[INFO] parsing page {page_idx} | url={page.url}")
            visited.add(page.url)

            # 等到有卡片或顯式等待
            try:
                page.wait_for_selector("div[data-test-target='review-card'], [data-automation='reviewCard']", timeout=8000)
            except PWTimeoutError:
                print("[WARN] reviewCard not found yet; try scroll more")
                human_scroll(page, steps=3)

            # 取得景點名稱（只取主名稱，不含 Unclaimed 等標籤）
            attraction = None
            try:
                h1 = page.locator("h1[data-test-target='mainH1']")
                if h1.count():
                    # 只取第一個 span 或文字節點
                    main_span = h1.first.locator("span").first
                    if main_span.count():
                        attraction = (main_span.text_content() or "").strip()
                    else:
                        # fallback: 取 h1 的第一個文字（排除 Unclaimed）
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

            reviews = parse_current_page(page, debug_dir, page_idx, target)
            # 加入景點名稱到每筆 review
            for r in reviews:
                r["attraction"] = attraction
            all_reviews.extend(reviews)

            # 下一頁
            next_clicked = False
            for sel in [
                "nav[aria-label='Pagination'] a[aria-label*='Next']",
                "a[aria-label='Next page']",
                "a[aria-label='Next']",
                "button[aria-label='Next']",
                "li[title='Next Page'] a",
                "a[data-page-number][aria-label*='Next']",
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() and loc.first.is_visible() and loc.first.is_enabled():
                        href_before = page.url
                        loc.first.click()
                        page.wait_for_load_state("domcontentloaded")
                        rsleep(0.8, 1.6)
                        if looks_like_verification(page):
                            print("\n[ACTION] 翻頁後又遇到驗證，請在視窗完成驗證，再按 Enter 繼續。")
                            input(">> 按 Enter 繼續… ")
                        if page.url in visited or page.url == href_before:
                            next_clicked = False
                        else:
                            next_clicked = True
                            break
                except Exception as e:
                    print(f"[WARN] next click failed via {sel}: {e}")

            if not next_clicked: break
            page_idx += 1

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

        browser.close()
        return all_reviews

def cli():
    import json

    base_url = "https://www.tripadvisor.com"
    json_path = Path("../shared/tripadv.json").resolve()

    # 載入 JSON
    with open(json_path, "r", encoding="utf-8") as f:
        urls = json.load(f)

    if not isinstance(urls, list):
        print(f"[ERROR] JSON 應該是 list，但得到 {type(urls)}")
        sys.exit(1)

    debug_dir = Path("debug_shots").resolve()
    out_json  = Path("reviews.json").resolve()
    out_csv   = Path("reviews.csv").resolve()

    print(f"[INFO] CWD={os.getcwd()}")
    print(f"[INFO] debug_dir={debug_dir}")
    print(f"[INFO] out_json={out_json}")
    print(f"[INFO] out_csv={out_csv}")

    processed_path = Path("processed_urls.txt").resolve()
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

    for rel_path in urls:
        full_url = base_url + rel_path
        if full_url in processed:
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

    print(f"[DONE] 所有 URL 處理完畢。結果已追加到 {out_csv}")


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(1)
