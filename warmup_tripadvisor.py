import argparse
import sys
import time
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://www.tripadvisor.com/"
DEFAULT_STATE = "./shared/ta_state.json"

COOKIE_BTN_PATTERN = re.compile(r"(Accept|Agree|I agree|OK)", re.I)

def main():
    parser = argparse.ArgumentParser(
        description="Warm up a human-verified TripAdvisor session and save storage_state (cookies/localStorage)."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Page to open first (default: TripAdvisor homepage).")
    parser.add_argument("--state", default=DEFAULT_STATE, help="Path to save storage_state JSON.")
    parser.add_argument("--width", type=int, default=1366)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--ua", default=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ))
    args = parser.parse_args()

    state_path = Path(args.state).expanduser().resolve()
    print(f"[warmup] Target URL : {args.url}")
    print(f"[warmup] Save state : {state_path}")

    with sync_playwright() as p:
        # 有頭模式，讓你手動破驗證
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            user_agent=args.ua,
            locale="en-US",
            viewport={"width": args.width, "height": args.height},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # 基本偽裝（降低被立即分流到驗證頁的機率；不保證完全避免）
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        page = ctx.new_page()
        page.set_default_timeout(30000)
        page.goto(args.url, wait_until="domcontentloaded")

        # 嘗試自動點 Cookie 同意（若有）
        try:
            consent = page.get_by_role("button", name=COOKIE_BTN_PATTERN)
            if consent.count() and consent.first.is_visible():
                print("[warmup] Clicking cookie consent...")
                consent.first.click()
                time.sleep(0.8)
        except Exception:
            pass

        print("\n======================================================")
        print("  在這個瀏覽器視窗中，請手動完成：")
        print("  1) TripAdvisor 的拼圖/驗證")
        print("  2) Cookie 同意（若再次出現）")
        print("  3) 視需要導航到你常用的頁面（可增加後續穩定性）")
        print("  完成後回到此終端機，按 Enter 以儲存 session。")
        print("======================================================\n")
        input(">> 完成驗證後按 Enter 以儲存 ta_state.json ... ")

        # 儲存 session
        state_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(state_path))
        print(f"[warmup] Saved storage_state to: {state_path}")

        browser.close()
    print("[warmup] Done.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
