"""Capture a running app in Chromium with cross-origin web fonts blocked."""

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit


def _origin(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be an HTTP or HTTPS address")
    default_port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname, parts.port or default_port


def _route_fonts(route, origin):
    request = route.request
    if request.resource_type != "font":
        route.continue_()
    elif _origin(request.url) != origin:
        route.abort()
    else:
        # Intercept direct font requests and refuse redirects out of this handler.
        response = route.fetch(max_redirects=0)
        if 300 <= response.status < 400:
            route.abort()
        else:
            route.fulfill(response=response)


def capture(url, output, width, height, ready, timeout):
    from playwright.sync_api import sync_playwright

    origin = _origin(url)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height}, service_workers="block"
            )
            context.route("**/*", lambda route: _route_fonts(route, origin))
            page = context.new_page()
            page.set_default_timeout(timeout)
            response = page.goto(url, wait_until="load")
            if response is None or not response.ok:
                status = response.status if response is not None else "no response"
                raise ValueError(f"page navigation returned {status}")
            if ready:
                page.locator(ready).wait_for(state="visible")
            page.wait_for_function("document.fonts.status === 'loaded'")
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=output, full_page=True, animations="disabled")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--ready", help="selector for an element that is visible when ready")
    parser.add_argument("--timeout", type=int, default=30000, help="timeout in milliseconds")
    args = parser.parse_args()
    if min(args.width, args.height, args.timeout) <= 0:
        parser.error("viewport dimensions and timeout must be positive")
    try:
        from playwright.sync_api import Error
    except ImportError:
        print("Install Playwright and its Chromium browser. See verify/README.md.", file=sys.stderr)
        return 2
    try:
        capture(args.url, args.output, args.width, args.height, args.ready, args.timeout)
    except (Error, OSError, ValueError) as error:
        print(f"Screenshot failed: {error}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
