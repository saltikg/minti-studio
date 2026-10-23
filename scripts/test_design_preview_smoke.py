#!/usr/bin/env python3
"""Small browser smoke test for the shared Crop/Edit Shorts design preview."""

from __future__ import annotations

import argparse
import os
import threading
from contextlib import contextmanager

os.environ.setdefault("GENERATE_V2_ALL", "true")

from playwright.sync_api import Page, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from app.video_shorts.services.db import get_db_readonly


def _admin_video_context(video_id: int | None) -> tuple[int, str, str]:
    conn = get_db_readonly()
    try:
        where = "AND v.id = ?" if video_id is not None else ""
        params = [video_id] if video_id is not None else []
        row = conn.execute(
            f"""
            SELECT v.id, CAST(v.owner_user_id AS VARCHAR), CAST(v.brand_id AS VARCHAR)
            FROM youtube_videos v
            JOIN shorts_users u
              ON CAST(u.id AS VARCHAR) = CAST(v.owner_user_id AS VARCHAR)
            WHERE lower(COALESCE(u.role, '')) IN ('admin', 'superadmin')
              AND COALESCE(v.transcript_status, '') = 'done'
              {where}
            ORDER BY v.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            target = f" {video_id}" if video_id is not None else ""
            raise RuntimeError(f"No transcript-ready admin test video{target} was found")
        brand_id = str(row[2] or "").strip()
        if not brand_id:
            brand = conn.execute(
                """
                SELECT CAST(id AS VARCHAR)
                FROM shorts_brands
                WHERE CAST(owner_user_id AS VARCHAR) = ?
                ORDER BY COALESCE(is_default, false) DESC, created_at
                LIMIT 1
                """,
                [str(row[1])],
            ).fetchone()
            brand_id = str(brand[0] if brand else "").strip()
        if not brand_id:
            raise RuntimeError("Admin test video has no usable brand")
        return int(row[0]), str(row[1]), brand_id
    finally:
        conn.close()


@contextmanager
def _test_server(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _active_band_script(kind: str) -> str:
    ids = (
        ["shorts-preview-title-band", "orig-preview-title"]
        if kind == "title"
        else ["shorts-preview-subtitle-band", "orig-preview-caption"]
    )
    return """
      () => {
        const ids = %s;
        return ids.map((id) => document.getElementById(id)).find((el) => {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none'
            && style.visibility !== 'hidden' && !el.classList.contains('is-caption-paint-inactive');
        }) || null;
      }
    """ % ids


def _wait_for_preset(page: Page, preset: str) -> None:
    page.wait_for_function(
        """preset => {
          const el = ['shorts-preview-subtitle-band', 'orig-preview-caption']
            .map(id => document.getElementById(id))
            .find(node => node?.dataset.paintPreset === preset && node.querySelector('svg'));
          return !!el;
        }""",
        arg=preset,
        timeout=10_000,
    )


def _select_style(page: Page, key: str) -> None:
    selector = f"[data-style-template-card][data-style-template-key='{key}']"
    page.locator(selector).evaluate("element => element.click()")
    _wait_for_preset(page, key)


def _caption_effect(page: Page) -> dict:
    return page.evaluate(
        r"""() => {
          const band = ['shorts-preview-subtitle-band', 'orig-preview-caption']
            .map(id => document.getElementById(id))
            .find(el => el?.querySelector('svg') && getComputedStyle(el).display !== 'none'
              && !el.classList.contains('is-caption-paint-inactive'));
          const svg = band?.querySelector(':scope > svg');
          const active = svg?.querySelector('text[data-preview-run-active="1"]');
          const fill = active?.getAttribute('fill') || '';
          const match = fill.match(/^url\(#(.+)\)$/);
          const gradient = match ? svg.querySelector(`#${CSS.escape(match[1])}`) : null;
          return {
            preset: band?.dataset.paintPreset || '',
            fill,
            stops: Array.from(gradient?.querySelectorAll('stop') || []).map(stop =>
              (stop.getAttribute('stop-color') || '').toUpperCase()),
            rects: Array.from(svg?.querySelectorAll('rect') || []).map(rect =>
              (rect.getAttribute('fill') || '').toUpperCase()),
            visible: !!band && band.getBoundingClientRect().height > 0,
          };
        }"""
    )


def _print_pass(label: str) -> None:
    print(f"PASS {label}")


def run(video_id: int | None, headed: bool) -> None:
    app = create_app()
    selected_video, user_id, brand_id = _admin_video_context(video_id)
    client = app.test_client()
    with client.session_transaction(base_url="http://127.0.0.1") as session:
        session["vs_user_id"] = user_id
        session["vs_brand_id"] = brand_id
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    cookie = client.get_cookie(cookie_name, domain="127.0.0.1") or client.get_cookie(cookie_name)
    if not cookie:
        raise RuntimeError("Could not create the smoke-test session cookie")

    with _test_server(app) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base_url}])
        page = context.new_page()
        page.goto(f"{base_url}/video_shorts/generate/{selected_video}", wait_until="domcontentloaded")
        page.wait_for_selector("#clip-workbench-shell[data-generate-v2='1']", timeout=15_000)
        page.evaluate("() => window.setGenerateV2ActiveTab('edit', {persist: false})")
        page.wait_for_function("() => window.__generateV2EditModeActive === true")
        page.wait_for_function(
            """() => ['shorts-preview-title-band', 'orig-preview-title']
              .some(id => document.getElementById(id)?.dataset.captionDomOwner === 'measured-svg')""",
            timeout=10_000,
        )

        aspect = page.evaluate(
            """() => {
              const band = ['shorts-preview-title-band', 'orig-preview-title']
                .map(id => document.getElementById(id))
                .find(el => el?.dataset.captionDomOwner === 'measured-svg' && getComputedStyle(el).display !== 'none');
              const rect = band.getBoundingClientRect();
              const viewBox = band.querySelector(':scope > svg').viewBox.baseVal;
              return {band: rect.width / rect.height, viewBox: viewBox.width / viewBox.height};
            }"""
        )
        assert abs(aspect["band"] - aspect["viewBox"]) / aspect["viewBox"] <= 0.01, aspect
        _print_pass("title first-paint aspect matches viewBox")

        _select_style(page, "electric_lime")
        effect = _caption_effect(page)
        assert "#C6F24D" in effect["stops"], effect
        _print_pass("Electric Lime resolves to the scoped lime gradient")

        _select_style(page, "blue_pop")
        effect = _caption_effect(page)
        assert "#2E7FFF" in effect["rects"], effect
        _print_pass("Blue Pop resolves to the blue active-word pill")

        _select_style(page, "silver_pop")
        effect = _caption_effect(page)
        assert "#FFFFFF" in effect["stops"] and "#B5BBC4" in effect["stops"], effect
        _print_pass("Silver Pop resolves to the scoped silver gradient")

        before_height = page.evaluate(
            """() => ['shorts-preview-subtitle-band', 'orig-preview-caption']
              .map(id => document.getElementById(id)).find(el => el?.dataset.captionDomOwner === 'measured-svg'
                && getComputedStyle(el).display !== 'none').getBoundingClientRect().height"""
        )
        page.locator("[name='sub_font_size']").evaluate(
            """field => {
              field.value = String(Number(field.value || 40) + 8);
              field.dispatchEvent(new Event('input', {bubbles: true}));
              field.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
        page.wait_for_function(
            """oldHeight => {
              const band = ['shorts-preview-subtitle-band', 'orig-preview-caption']
                .map(id => document.getElementById(id)).find(el => el?.dataset.captionDomOwner === 'measured-svg'
                  && getComputedStyle(el).display !== 'none');
              return band && Math.abs(band.getBoundingClientRect().height - oldHeight) > 1;
            }""",
            arg=before_height,
        )
        _print_pass("caption font-size change updates measured band height")

        _select_style(page, "electric_lime")
        title = page.locator("#shorts-preview-title-band:visible, #orig-preview-title:visible").first
        box = title.bounding_box()
        assert box
        before_top = box["y"]
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 24, steps=4)
        page.mouse.up()
        page.wait_for_timeout(300)
        after_top = title.bounding_box()["y"]
        assert abs(after_top - before_top) > 5, (before_top, after_top)
        effect = _caption_effect(page)
        assert effect["visible"] and "#C6F24D" in effect["stops"], effect
        _print_pass("title drag stays committed and caption remains highlighted")

        duplicates = page.evaluate(
            """() => {
              const seen = new Set(); const duplicates = [];
              document.querySelectorAll('[id]').forEach(el => {
                if (seen.has(el.id)) duplicates.push(el.id); else seen.add(el.id);
              });
              return [...new Set(duplicates)];
            }"""
        )
        assert not duplicates, duplicates
        _print_pass("page contains no duplicate DOM ids")
        print(f"SMOKE_OK video={selected_video} user={user_id} brand={brand_id}")
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", type=int, help="Transcript-ready video owned by an admin user")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    run(args.video_id, args.headed)


if __name__ == "__main__":
    main()
