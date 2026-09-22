"""Optional browser observations. Never treats rendering as visual quality proof."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def inspect_html(content: str, output_dir: Path) -> dict:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parent / ".browsers"))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"rendered": False, "reason": "Playwright 미설치", "method": "unavailable"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block",
                                              accept_downloads=False, viewport={"width": 1280, "height": 900})
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(8000)
                # Network disabled; CSP also blocks script, frames, objects and resource fetches.
                csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; script-src 'none'; frame-src 'none'; form-action 'none'\">"
                page.set_content(csp + content, wait_until="domcontentloaded", timeout=8000)
                observation = page.evaluate("""() => {
                    const blocks = Array.from(document.querySelectorAll('h1,h2,h3,p,li,td,th,pre,blockquote'));
                    const hidden = blocks.filter(e => { const s=getComputedStyle(e); const r=e.getBoundingClientRect();
                        return s.display==='none'||s.visibility==='hidden'||s.opacity==='0'||!r.width||!r.height; });
                    return {visibleText:document.body.innerText, tableCount:document.querySelectorAll('table').length,
                      hiddenBlocks:hidden.map(e=>e.textContent), horizontalOverflow:document.documentElement.scrollWidth>innerWidth};
                }""")
                observation["markdownMarkerCandidates"] = re.findall(r"\*\*[^*\n]+\*\*", observation["visibleText"])
                output_dir.mkdir(parents=True, exist_ok=True)
                name = hashlib.sha256(content.encode()).hexdigest() + ".png"
                page.screenshot(path=str(output_dir / name), full_page=False, timeout=8000)
                return {"rendered": True, "method": "chromium_javascript_disabled", "viewport": [1280, 900],
                        "screenshot": name, "observations": observation,
                        "limitation": "스크립트/외부 자원 차단. 캡처는 첫 화면. 미적 품질·모든 잘림·겹침은 판정하지 않음."}
            finally:
                browser.close()
    except Exception as error:
        return {"rendered": False, "method": "browser_failed", "reason": str(error)[:300]}
