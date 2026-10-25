"""Capture all FinOps pages and chart toggles at a 1440x900 desktop viewport.

Start the app locally on 127.0.0.1:8501, then run:
    .\\.venv\\Scripts\\python.exe scripts\\screenshot.py
"""
from __future__ import annotations

import re
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "screenshots"
URL = os.getenv("CLOUD_COST_DASHBOARD_URL", "http://127.0.0.1:8501")
PAGES = [
    ("Overview", "overview"),
    ("Cost Trends", "cost_trends"),
    ("Service Breakdown", "service_breakdown"),
    ("Anomalies", "anomalies"),
    ("Optimization", "optimization"),
    ("Budgets & Forecasts", "budgets_forecasts"),
    ("Ask AI", "ask_ai"),
    ("Billing Records", "billing_records"),
]


def wait_until_rendered(page, *, expect_chart: bool = False) -> None:
    page.locator('[data-testid="stAppViewContainer"]').wait_for(state="visible", timeout=60_000)
    page.wait_for_function(
        """() => {
          const spinner = document.querySelector('[data-testid="stSpinner"]');
          const status = document.querySelector('[data-testid="stStatusWidget"]');
          const running = status && (/running/i.test(status.innerText || '') || status.getAttribute('data-state') === 'running');
          const stopButton = [...document.querySelectorAll('button,[aria-label]')].some(element =>
            /stop/i.test((element.getAttribute('aria-label') || '') + ' ' + (element.innerText || '')) && element.getClientRects().length > 0);
          const main = document.querySelector('main.stMain');
          const workText = main && /Checking the selected cost data/i.test(main.innerText || '');
          return Boolean(main) && !spinner && !running && !stopButton && !workText;
        }""",
        timeout=60_000,
    )
    if expect_chart:
        page.wait_for_function(
            """() => [...(document.querySelector('main.stMain') || document).querySelectorAll('canvas')]
              .some(canvas => { const box = canvas.getBoundingClientRect(); return box.width > 0 && box.height > 0; })""",
            timeout=60_000,
        )
    # Let ECharts' entrance/update animation complete before reading pixels.
    page.wait_for_timeout(2_000)


def capture_full_page(page, path: Path) -> dict[str, int]:
    """Expand Streamlit's internal scroll panes, then capture one true full-page image."""
    page.evaluate("""() => {
      if (!document.getElementById('finops-full-page-capture')) {
        const style = document.createElement('style');
        style.id = 'finops-full-page-capture';
        style.textContent = `html, body, #root, [data-testid="stApp"], [data-testid="stAppViewContainer"] {
          height: auto !important; min-height: 100vh !important; overflow: visible !important; position: relative !important;
        }
        main.stMain { height: auto !important; max-height: none !important; overflow: visible !important; }
        [data-testid="stSidebar"] { height: auto !important; max-height: none !important; overflow: visible !important; }
        [data-testid="stBottom"], [data-testid="stChatInput"],
        [data-testid="stBottomBlockContainer"], .stChatInput {
          position: static !important; bottom: auto !important; width: 100% !important;
        }`;
        document.head.appendChild(style);
      }
      const main = document.querySelector('main.stMain');
      const sidebar = document.querySelector('[data-testid="stSidebar"]');
      if (main && sidebar) {
        const fullHeight = Math.max(main.scrollHeight, main.getBoundingClientRect().height);
        sidebar.style.height = `${fullHeight}px`;
        sidebar.style.minHeight = `${fullHeight}px`;
      }
    }""")
    page.wait_for_timeout(250)
    dimensions = page.evaluate("""() => ({
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight
    })""")
    path.parent.mkdir(exist_ok=True)
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    return {"scrollHeight": int(dimensions["height"]), "viewportWidth": int(dimensions["width"]),
            "screenshotHeight": int(dimensions["height"])}


def save(page, name: str, error_pages: list[str], *, expect_chart: bool = True) -> dict[str, int]:
    wait_until_rendered(page, expect_chart=expect_chart)
    body_text = page.locator("body").inner_text()
    if any(token in body_text for token in ("StreamlitAPIException:", "ModuleNotFoundError:",
                                            "Traceback (most recent call last):", "This app has encountered an error")):
        error_pages.append(name)
    return capture_full_page(page, OUT / f"{name}.png")


def click_sidebar_page(page, label: str) -> None:
    page.get_by_role("button", name=re.compile(re.escape(label))).click(timeout=20_000)
    wait_until_rendered(page)


def main() -> None:
    errors: list[str] = []
    layout: dict[str, dict[str, int]] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.get_by_text("Cloud cost overview", exact=True).wait_for(timeout=60_000)
        wait_until_rendered(page)

        for label, slug in PAGES:
            if label != "Overview":
                click_sidebar_page(page, label)
            layout[slug] = save(page, slug, errors, expect_chart=label not in ("Ask AI", "Billing Records"))

            if label == "Overview":
                # The top ranked-service tooltip must include its usage-type context.
                bar_frame = next(frame for frame in page.frames if "/component/streamlit_echarts.st_echarts/" in frame.url)
                bar_canvas = bar_frame.locator("canvas")
                bar_canvas.wait_for(state="visible", timeout=30_000)
                bar_canvas.hover(position={"x": 500, "y": 32})
                bar_frame.get_by_text("Largest usage type", exact=False).wait_for(timeout=10_000)
                layout["overview_service_tooltip"] = save(page, "overview-service-tooltip", errors)
                bar_canvas.hover(position={"x": 5, "y": 5})
                wait_until_rendered(page)
                # Verify the native bar click event really filters the lower composition.
                bar_canvas.click(position={"x": 500, "y": 32})
                page.get_by_text("Filtered to Amazon EC2", exact=False).wait_for(timeout=30_000)
                layout["overview_service_filter"] = save(page, "overview-service-filter", errors)
                page.get_by_role("button", name=re.compile("Filtered to Amazon EC2")).click()
                wait_until_rendered(page)
                group = page.get_by_role("radiogroup", name="Composition")
                for choice in ("Service", "Team", "Account", "Region"):
                    group.get_by_text(choice, exact=True).click(timeout=10_000)
                    layout[f"overview_composition_{choice.lower()}"] = save(page, f"overview-composition-{choice.lower()}", errors)
                group.get_by_text("Service", exact=True).click(timeout=10_000)
                wait_until_rendered(page)

            if label == "Service Breakdown":
                for tab in ("Service", "Account", "Region", "Team", "Environment"):
                    page.get_by_role("tab", name=tab, exact=True).click(timeout=10_000)
                    layout[f"service_breakdown_{tab.lower()}"] = save(page, f"service-breakdown-{tab.lower()}", errors)

            if label == "Ask AI":
                chip = page.get_by_role("button", name="Which service costs the most?", exact=True)
                chip.click(timeout=10_000)
                page.get_by_text("How I got this", exact=True).wait_for(timeout=120_000)
                page.get_by_text("How I got this", exact=True).click()
                page.get_by_text("SELECT service", exact=False).wait_for(timeout=10_000)
                layout["ask_ai_chip"] = save(page, "ask-ai-chip", errors, expect_chart=False)
                prompt = page.get_by_placeholder("Ask about the selected billing data")
                prompt.fill("Show spend by region")
                prompt.press("Enter")
                page.get_by_text("How I got this", exact=True).wait_for(timeout=120_000)
                wait_until_rendered(page)
                layout["ask_ai_typed"] = save(page, "ask-ai-typed", errors, expect_chart=False)
                prompt = page.get_by_placeholder("Ask about the selected billing data")
                prompt.fill("What color is the AWS logo?")
                prompt.press("Enter")
                page.get_by_text("I couldn't match that question", exact=False).wait_for(timeout=120_000)
                wait_until_rendered(page)
                layout["ask_ai_unanswerable"] = save(page, "ask-ai-unanswerable", errors, expect_chart=False)

        browser.close()

    print(f"Screenshots saved: {OUT}")
    for name, metrics in layout.items():
        print(f"{name}: viewport={metrics['viewportWidth']}x900, pageHeight={metrics['screenshotHeight']}px")
    print(f"Streamlit error pages: {len(errors)}")
    if errors:
        print("Error page identifiers: " + ", ".join(errors))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
