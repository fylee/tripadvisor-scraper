import asyncio
import json
import sys
import time
from datetime import datetime
from typing import List, Set

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

START_URL_DEFAULT = (
    "https://www.tripadvisor.com/Attractions-g295415-Activities-oa0-Luang_Prabang_Luang_Prabang_Province.html"
)
OUTFILE = "TripAdv_Atts_List_test.json"

# ---- 可調參數 ----
HEADLESS = True          # 需要觀察行為可改 False
LOAD_TIMEOUT_MS = 40_000
SCROLL_STEP_PX = 900
SCROLL_SETTLE_MS = 600
PAUSE_BETWEEN_PAGES_MS = 1200  # 翻頁之間稍等，降低被風控機率


import asyncio, os, json

STORAGE_STATE = "ta_state.json"

CHALLENGE_HOST_HINTS = (
    "captcha-delivery.com", "geo.captcha-delivery.com", "ct.captcha-delivery.com",
    "arkoselabs", "hcaptcha", "datadome", "verify", "verification"
)

def _looks_like_challenge_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in CHALLENGE_HOST_HINTS)

async def wait_manual_and_save(page, context, label="challenge"):
    """停住等人工完成驗證，完成後存 cookie/state。"""
    await page.screenshot(path=f"{label}.png", full_page=True)
    print(f"\n[ACTION] 出現驗證頁（{label}）。請在彈出的瀏覽器完成拼圖，完成後回終端機按 Enter 繼續…")
    try:
        await page.bring_to_front()
    except Exception:
        pass
    # 等人按 Enter（不阻塞事件迴圈）
    await asyncio.get_event_loop().run_in_executor(None, input, ">> 完成後按 Enter… ")

    # 最長等 10 分鐘，直到不是驗證頁
    try:
        await page.wait_for_function(
            "() => !/captcha|verify|Verification Required/i.test(document.body.innerText) "
            "&& !/captcha-delivery|arkoselabs|hcaptcha|datadome/i.test(location.href)",
            timeout=10 * 60 * 1000
        )
    except Exception:
        pass

    # 保存 cookie/state
    try:
        await context.storage_state(path=STORAGE_STATE)
        print(f"[INFO] Storage state saved -> {STORAGE_STATE}")
    except Exception as e:
        print(f"[WARN] Save storage state failed: {e}")


async def looks_like_verification(page) -> bool:
    url = page.url.lower()
    if any(k in url for k in ("captcha", "verify", "captcha-delivery", "arkoselabs", "geetest", "hcaptcha", "datadome")):
        return True
    try:
        txt = await page.text_content("body")
        return bool(txt and "Verification Required" in txt)
    except Exception:
        return False

async def ensure_verified(page, context, label="first"):
    """偵測到驗證時，提示人工解題並保存 cookie/state。"""
    if not await looks_like_verification(page):
        return
    await page.screenshot(path=f"verify_{label}.png", full_page=True)
    print("\n[ACTION] TripAdvisor 觸發驗證，請在彈出的瀏覽器完成拼圖。完成後回到終端機按 Enter 繼續…")
    try:
        await page.bring_to_front()
    except Exception:
        pass
    # 等人按 Enter（不想阻塞 event loop 可用 run_in_executor）
    await asyncio.get_event_loop().run_in_executor(None, input, ">> 完成後按 Enter… ")
    # 再等驗證畫面消失（最多 10 分鐘）
    try:
        await page.wait_for_function(
            "() => !document.body.innerText.includes('Verification Required')",
            timeout=10*60*1000
        )
    except Exception:
        pass
    # 保存 state（含 datadome 等 cookie）
    try:
        await context.storage_state(path=STORAGE_STATE)
        print(f"[INFO] Storage state saved to {STORAGE_STATE}")
    except Exception as e:
        print(f"[WARN] Save storage state failed: {e}")


async def scroll_to_bottom(page):
    """緩慢滾到底，確保懶載入元素出現"""
    prev = -1
    while True:
        curr = await page.evaluate("() => document.body.scrollHeight")
        if curr == prev:
            break
        prev = curr
        await page.mouse.wheel(0, SCROLL_STEP_PX)
        await page.wait_for_timeout(SCROLL_SETTLE_MS)


async def extract_restaurant_links(page) -> List[str]:
    """
    抽出本頁所有餐廳「評論頁」連結：
    目標 href 形如：/Restaurant_Review-g295415-dXXXXX-Reviews-*.html
    """
    # 主要 selector
    links = await page.eval_on_selector_all(
        "a[href^='/Attraction_Review-']",
        "els => els.map(e => e.getAttribute('href'))"
    )
    print(f"[DEBUG] Found {len(links)} links in this page.")

    # 轉為絕對網址、過濾 None
    full = []
    for href in links:
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.tripadvisor.com" + href
        full.append(href)
    return full

async def find_and_click_next(page, context) -> bool:
    selectors = [
        "a[data-smoke-attr='pagination-next-arrow']",
        "a[aria-label*='Next page' i]",
        "a[aria-label*='Next' i]",
        "span:has(a[aria-label*='Next' i]) a",
        "a.ui_button.nav.next.primary",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count():
            try:
                await loc.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
                # 若翻頁後出現驗證 → 請人處理
                await ensure_verified(page, page.context, label="next")
                await scroll_to_bottom(page)
                return True
            except Exception:
                continue
    return False

async def run(start_url: str):
    async with async_playwright() as p:
        # 建議先 headful + Chrome 觀察；通過後可改 headless
        browser = await p.chromium.launch(
            headless=False, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )

        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

        if os.path.exists(STORAGE_STATE):
            context = await browser.new_context(
                storage_state=STORAGE_STATE,
                user_agent=ua, viewport={"width": 1366, "height": 900},
                locale="en-US", java_script_enabled=True,
                bypass_csp=True, ignore_https_errors=True,
            )
        else:
            context = await browser.new_context(
                user_agent=ua, viewport={"width": 1366, "height": 900},
                locale="en-US", java_script_enabled=True,
                bypass_csp=True, ignore_https_errors=True,
            )

        # 簡單的指紋修飾
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en']});
        """)

        # （可選）擋常見廣告/追蹤，減少干擾
        AD_BLOCK = True
        AD_DOMAINS = (
            "doubleclick.net", "googlesyndication.com", "googletagservices.com",
            "google-analytics.com", "googletagmanager.com", "ads.as.criteo.com",
            "adnxs.com", "taboola.com", "rubiconproject.com", "facebook.com/tr",
        )
        if AD_BLOCK:
            async def _router(route, request):
                url = request.url
                # 別擋驗證用網域
                if any(x in url for x in ("captcha-delivery.com", "datadome")):
                    return await route.continue_()
                if any(d in url for d in AD_DOMAINS):
                    return await route.abort()
                return await route.continue_()
            await context.route("**/*", _router)

        page = await context.new_page()

        # 只記錄「主框架」的 document，避免 iframe 廣告洗版
        def _on_resp(resp):
            try:
                if resp.request.resource_type == "document" and resp.frame == page.main_frame:
                    print(f"[RESP] {resp.status} {resp.url} (MAIN)")
            except Exception:
                pass
        page.on("response", _on_resp)

        async def goto_with_challenge(url: str, label: str):
            print(f"[INFO] Navigating to {url}")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_selector("body", state="attached", timeout=60_000)
            html_now = await page.content()
            try:
                title = await page.title()
            except Exception:
                title = ""
            status = resp.status if resp else None
            await page.screenshot(path=f"{label}_after_goto.png", full_page=True)
            print(f"[DEBUG] {label}: url={page.url} status={status} title='{title}' html_len={len(html_now)}")

            if (status == 403 or _looks_like_challenge_url(page.url)
                or "Verification Required" in html_now
                or "Access blocked" in html_now):
                if "Access blocked" in html_now:
                    print("\n[ACTION] 偵測到『Access blocked』。請換乾淨的住宅/行動 IP 或關閉公司 VPN/代理，"
                          "在視窗中完成任何拼圖後回終端機按 Enter。")
                await wait_manual_and_save(page, context, label=f"{label}_challenge")

                print("[INFO] Retry same URL after manual verification…")
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_selector("body", state="attached", timeout=60_000)
                await page.screenshot(path=f"{label}_retry.png", full_page=True)

        # ---------- 首頁處理 ----------
        await goto_with_challenge(start_url, "first")

        # 不等 networkidle，直接等我們需要的元素
        # 先等分頁導覽或評論卡出現其一
        try:
            await page.wait_for_selector(
                "nav[aria-label='Pagination'], div[data-test-target='review-card'], [data-automation='reviewCard']",
                timeout=30_000
            )
        except Exception:
            pass  # 有些城市清單頁需要先捲動才渲染

        # 滑動讓卡片/連結 lazyload
        await scroll_to_bottom(page)
        await page.wait_for_timeout(800)
        await page.screenshot(path="tripadv_food_list.png", full_page=True)

        # ---------- 蒐集連結 + 翻頁 ----------
        all_links: Set[str] = set()
        page_idx = 1

        while True:
            print(f"[INFO] On page {page_idx}: {page.url}")
            links = await extract_restaurant_links(page)
            before = len(all_links)
            all_links.update(links)
            print(f"       found {len(links)} links, total unique {len(all_links)} (+{len(all_links)-before})")

            moved = await find_and_click_next(page, context)
            if not moved:
                print("[INFO] No next page (or blocked). Stop.")
                await page.screenshot(path=f"tripadv_food_list_p{page_idx}.png", full_page=True)
                with open(f"tripadv_food_list_p{page_idx}.html", "w", encoding="utf-8") as f:
                    f.write(await page.content())
                break

            # 翻頁後若又遇驗證：等待你手動通過
            html_after = await page.content()
            if (_looks_like_challenge_url(page.url)
                or "Verification Required" in html_after
                or "Access blocked" in html_after):
                await wait_manual_and_save(page, context, label=f"challenge_p{page_idx+1}")

            # 也不要等 networkidle，直接捲動觸發渲染
            await scroll_to_bottom(page)
            await page.wait_for_timeout(600)
            page_idx += 1
            break

        # 保存 state 以便下次沿用
        try:
            await context.storage_state(path=STORAGE_STATE)
            print(f"[INFO] Storage state saved -> {STORAGE_STATE}")
        except Exception as e:
            print(f"[WARN] Save storage state failed: {e}")

        await browser.close()

        out = {
            "source": start_url,
            "collected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "count": len(all_links),
            "links": sorted(all_links),
        }
        with open(OUTFILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[DONE] Saved {len(all_links)} links to {OUTFILE}")





if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else START_URL_DEFAULT
    asyncio.run(run(url))
