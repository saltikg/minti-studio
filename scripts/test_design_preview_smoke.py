#!/usr/bin/env python3
"""Browser smoke test for the shared Crop/Edit Shorts design preview."""

from __future__ import annotations

import argparse
import os
import threading
from contextlib import contextmanager

os.environ.setdefault("GENERATE_V2_ALL", "true")

from playwright.sync_api import Browser, Page, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from app.video_shorts.services.db import get_db_readonly


VIEWPORTS = ((1280, 720), (1920, 1080), (2560, 1440))
DPRS = (1, 2)


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
    card = page.locator(selector)
    assert card.count() == 1, {"style": key, "card_count": card.count()}
    card.evaluate("element => element.click()")
    page.wait_for_function(
        "preset => document.getElementById('subtitlePresetValue')?.value === preset",
        arg=key,
        timeout=10_000,
    )
    _wait_for_preset(page, key)


def _caption_effect(page: Page) -> dict:
    return page.evaluate(
        r"""() => {
          const band = ['shorts-preview-subtitle-band', 'orig-preview-caption']
            .map(id => document.getElementById(id))
            .find(el => el?.querySelector('svg') && getComputedStyle(el).display !== 'none'
              && !el.classList.contains('is-caption-paint-inactive'));
          const svg = band?.querySelector(':scope > svg');
          const active = svg?.querySelector('text[data-preview-run-active="1"]:not([data-preview-shadow="1"])');
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


def _print_pass(variant: str, label: str) -> None:
    print(f"PASS [{variant}] {label}")


def _assert_make_clip_add_keeps_exact_word_indexes(page: Page, variant: str) -> None:
    page.evaluate("() => window.setGenerateV2ActiveTab('transcript', {persist: false})")
    page.wait_for_selector("[data-generate-v2-text-document] .generate-v2-text-word")
    result = page.evaluate(
        """() => {
          const documentEl = document.querySelector('[data-generate-v2-text-document]');
          const paragraph = Array.from(documentEl.querySelectorAll('.generate-v2-text-paragraph'))
            .find(node => node.querySelectorAll('.generate-v2-text-word').length >= 2);
          const spans = Array.from(paragraph?.querySelectorAll('.generate-v2-text-word') || []);
          if (spans.length < 2) return {error: 'no selectable transcript sentence'};
          const first = spans[0];
          const last = spans[spans.length - 1];
          const selection = window.getSelection();
          const range = document.createRange();
          range.setStart(first.firstChild, 0);
          range.setEnd(last.firstChild, last.firstChild.textContent.length);
          selection.removeAllRanges();
          selection.addRange(range);
          documentEl.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
          const add = document.querySelector('[data-generate-v2-text-add]');
          if (!add || add.disabled) return {error: 'Add button unavailable'};
          add.click();
          const part = window.__generateV2TextParts?.[0] || null;
          const highlighted = Array.from(documentEl.querySelectorAll('.generate-v2-text-word.is-added'))
            .map(node => Number(node.dataset.wordIndex));
          return {
            first: Number(first.dataset.wordIndex),
            last: Number(last.dataset.wordIndex),
            part,
            highlighted,
          };
        }"""
    )
    assert not result.get("error"), result
    expected = list(range(result["first"], result["last"] + 1))
    assert result["part"]["start_word_index"] == result["first"], result
    assert result["part"]["end_word_index"] == result["last"], result
    assert result["highlighted"] == expected, result
    _print_pass(variant, "Make a Clip Add preserves exact selected word indexes")
    page.evaluate("() => document.querySelector('[data-generate-v2-text-clear-parts]')?.click()")
    page.evaluate("() => window.setGenerateV2ActiveTab('edit', {persist: false})")
    page.wait_for_function("() => window.__generateV2EditModeActive === true")


def _assert_stubbed_regenerate(page: Page, variant: str) -> None:
    card = page.locator(".clip-card:has(video):has(.generate-v2-regenerate-btn)").first
    if card.count() == 0:
        # The smoke server may not have the production media mount, even when
        # the selected clip is rendered in production. Hydrate one existing
        # card with the same controls so the real regenerate client flow can
        # still be exercised without starting a render.
        card = page.locator(".clip-card[data-plan-index]").first
        if card.count() == 0:
            page.evaluate(
                """() => {
                  const repository = document.getElementById('clip-card-repository')
                    || document.getElementById('clip-management-panel');
                  if (!repository) throw new Error('Smoke page has no clip card repository');
                  const card = document.createElement('div');
                  card.className = 'clip-card';
                  card.dataset.planIndex = 'smoke-regenerate';
                  card.dataset.regenerateUrl = `${window.location.pathname}/clip/smoke-regenerate/regenerate`;
                  card.innerHTML = '<div class="clip-card-media"></div>';
                  repository.appendChild(card);
                }"""
            )
            card = page.locator(".clip-card[data-plan-index='smoke-regenerate']")
        assert card.count() == 1, "Could not prepare a clip card for regenerate smoke testing"
        card.evaluate(
            """card => {
              card.dataset.status = 'created';
              card.dataset.outdated = '0';
              if (!card.dataset.regenerateUrl) {
                card.dataset.regenerateUrl = `${window.location.pathname}/clip/${card.dataset.planIndex}/regenerate`;
              }
              const media = card.querySelector('.clip-card-media');
              if (!media) throw new Error('Smoke clip card has no media container');
              media.innerHTML = `
                <div class="generate-v2-media-shell">
                  <div class="generate-v2-media-column">
                    <video src="/smoke-initial.mp4?v=smoke-regenerate-v1"
                      poster="/smoke-initial.jpg?v=smoke-regenerate-v1"></video>
                    <div class="generate-v2-render-overlay d-none">
                      <span class="generate-v2-render-overlay-label"></span>
                    </div>
                    <div class="clip-card-media-actions generate-v2-primary-actions">
                      <a class="generate-v2-open-btn" href="/smoke-initial.mp4?v=smoke-regenerate-v1"></a>
                      <a class="generate-v2-download-btn" href="/smoke-download?v=smoke-regenerate-v1"></a>
                      <button type="button" class="generate-v2-regenerate-btn"
                        data-plan-index="${card.dataset.planIndex}"
                        data-regenerate-url="${card.dataset.regenerateUrl}">
                        <i class="bi bi-arrow-clockwise"></i><span>Regenerate</span>
                      </button>
                    </div>
                  </div>
                </div>`;
              initGenerateV2RegenerateControls();
            }"""
        )
    plan_index = str(card.get_attribute("data-plan-index") or "").strip()
    assert plan_index, "Rendered clip card has no plan index"
    initial_src = str(card.locator("video").get_attribute("src") or "")
    initial_poster = str(card.locator("video").get_attribute("poster") or "")
    assert initial_src, "Rendered clip card has no video URL"
    new_version = "smoke-regenerate-v2"
    separator = "&" if "?" in initial_src else "?"
    new_src = f"{initial_src}{separator}smoke_version={new_version}"
    poster_separator = "&" if "?" in initial_poster else "?"
    new_poster = (
        f"{initial_poster}{poster_separator}smoke_version={new_version}"
        if initial_poster else ""
    )
    status_calls = {"count": 0}

    def fulfill_regenerate(route) -> None:
        route.fulfill(
            status=202,
            content_type="application/json",
            body='{"success":true,"job_id":"stub-regenerate-job","status":"processing"}',
        )

    def fulfill_status(route) -> None:
        status_calls["count"] += 1
        if status_calls["count"] == 1:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"id":"stub-regenerate-job","status":"processing"}',
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            json={
                "id": "stub-regenerate-job",
                "status": "done",
                "media": {
                    "plan_index": plan_index,
                    "clip_filename": f"stub-{plan_index}.mp4",
                    "media_version": new_version,
                    "video_url": new_src,
                    "poster_url": new_poster,
                    "download_url": f"/stub-download/{plan_index}?v={new_version}",
                    "needs_regenerate": False,
                    "regenerate_reason": "",
                },
            },
        )

    regenerate_pattern = "**/video_shorts/generate/*/clip/*/regenerate"
    status_pattern = "**/*stub-regenerate-job*"
    # The terminal handler refreshes the card from the page before reapplying
    # terminal media. Make that stubbed server page represent the completed
    # render, as the real endpoint does.
    page_html = page.content()
    page_html = page_html.replace(
        f'data-plan-index="{plan_index}"',
        f'data-plan-index="{plan_index}" data-media-version="{new_version}"',
    )
    for old_url, completed_url in ((initial_src, new_src), (initial_poster, new_poster)):
        if not old_url or not completed_url:
            continue
        page_html = page_html.replace(old_url, completed_url)
        page_html = page_html.replace(
            old_url.replace("&", "&amp;"), completed_url.replace("&", "&amp;")
        )

    def fulfill_page_refresh(route) -> None:
        route.fulfill(status=200, content_type="text/html", body=page_html)

    page.route(regenerate_pattern, fulfill_regenerate)
    page.route(status_pattern, fulfill_status)
    page.route(page.url, fulfill_page_refresh)
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        card.locator(".generate-v2-regenerate-btn").evaluate("button => button.click()")
        for _ in range(100):
            if status_calls["count"] >= 2:
                break
            page.wait_for_timeout(100)
        assert status_calls["count"] >= 2, status_calls
        page.wait_for_function(
            """({plan, version}) => {
              const card = document.querySelector(`.clip-card[data-plan-index='${plan}']`);
              const video = card?.querySelector('video');
              return card?.dataset.mediaVersion === version
                && video?.getAttribute('src')?.includes(`smoke_version=${version}`);
            }""",
            arg={"plan": plan_index, "version": new_version},
            timeout=30_000,
        )
        result = page.evaluate(
            """({plan, version}) => {
              const card = document.querySelector(`.clip-card[data-plan-index='${plan}']`);
              const overlay = card?.querySelector('.generate-v2-render-overlay');
              const video = card?.querySelector('video');
              return {
                hasCard: !!card,
                isRendering: card?.querySelector('.generate-v2-media-column')?.classList.contains('is-rendering'),
                overlayHidden: overlay?.classList.contains('d-none'),
                mediaVersion: card?.dataset.mediaVersion || '',
                videoSrc: video?.getAttribute('src') || '',
                expectedVersion: version,
              };
            }""",
            arg={"plan": plan_index, "version": new_version},
        )
        assert result["hasCard"] and not result["isRendering"], result
        assert result["overlayHidden"], result
        assert result["mediaVersion"] == new_version, result
        assert f"smoke_version={new_version}" in result["videoSrc"], result
        _print_pass(variant, "stubbed regenerate processing-to-done clears overlay and updates media version")
    finally:
        page.unroute(page.url, fulfill_page_refresh)
        page.unroute(status_pattern, fulfill_status)
        page.unroute(regenerate_pattern, fulfill_regenerate)


def _assert_server_geometry_round_trip(page: Page, variant: str, kind: str) -> None:
    result = page.evaluate(
        """kind => {
          const ids = kind === 'title'
            ? ['shorts-preview-title-band', 'orig-preview-title']
            : ['shorts-preview-subtitle-band', 'orig-preview-caption'];
          const band = ids.map(id => document.getElementById(id)).find(el => {
            if (el?.dataset.captionDomOwner !== 'measured-svg') return false;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none'
              && style.visibility !== 'hidden'
              && !el.classList.contains('is-caption-paint-inactive');
          });
          const geometry = window.getCropEditorPreviewGeometry?.();
          if (!band || !geometry?.frame) return {error: 'missing active measured band or geometry'};
          const bandRect = band.getBoundingClientRect();
          const frameRect = geometry.frame.getBoundingClientRect();
          const expected = JSON.parse(band.dataset.renderBbox || '[]');
          const actual = [
            geometry.clientToRenderX(bandRect.left - frameRect.left),
            geometry.clientToRenderY(bandRect.top - frameRect.top),
            geometry.clientToRenderX(bandRect.right - frameRect.left),
            geometry.clientToRenderY(bandRect.bottom - frameRect.top),
          ];
          return {expected, actual, renderWidth: geometry.renderWidth,
            renderHeight: geometry.renderHeight, dpr: window.devicePixelRatio};
        }""",
        kind,
    )
    assert not result.get("error"), result
    expected = result["expected"]
    actual = result["actual"]
    assert len(expected) == 4, result
    expected_values = (expected[0], expected[1], expected[2] - expected[0])
    actual_values = (actual[0], actual[1], actual[2] - actual[0])
    tolerances = (result["renderWidth"] * 0.005, result["renderHeight"] * 0.005,
                  result["renderWidth"] * 0.005)
    deltas = tuple(abs(left - right) for left, right in zip(expected_values, actual_values))
    assert all(delta <= tolerance for delta, tolerance in zip(deltas, tolerances)), {
        "kind": kind, "expected_left_top_width": expected_values,
        "actual_left_top_width": actual_values, "deltas": deltas,
        "tolerances": tolerances, "dpr": result["dpr"],
    }
    _print_pass(variant, f"{kind} band render-coordinate round trip")


def _run_variant(browser: Browser, *, base_url: str, cookie_name: str,
                 cookie_value: str, selected_video: int, width: int,
                 height: int, dpr: int) -> None:
    variant = f"{width}x{height} DPR{dpr}"
    context = browser.new_context(viewport={"width": width, "height": height},
                                  device_scale_factor=dpr)
    try:
        context.add_cookies([{"name": cookie_name, "value": cookie_value, "url": base_url}])
        page = context.new_page()
        page.on("pageerror", lambda error: print(f"PAGE_ERROR [{variant}] {error}"))
        page.on(
            "console",
            lambda message: print(f"CONSOLE_ERROR [{variant}] {message.text}")
            if message.type == "error" else None,
        )
        page.goto(f"{base_url}/video_shorts/generate/{selected_video}", wait_until="domcontentloaded")
        page.wait_for_selector("#clip-workbench-shell[data-generate-v2='1']", timeout=15_000)
        page.evaluate("() => window.setGenerateV2ActiveTab('edit', {persist: false})")
        page.wait_for_function("() => window.__generateV2EditModeActive === true")
        page.wait_for_function(
            """() => ['shorts-preview-title-band', 'orig-preview-title']
              .some(id => document.getElementById(id)?.dataset.captionDomOwner === 'measured-svg')""",
            timeout=10_000,
        )
        page.wait_for_function(
            """() => ['shorts-preview-subtitle-band', 'orig-preview-caption']
              .some(id => document.getElementById(id)?.dataset.captionDomOwner === 'measured-svg')""",
            timeout=10_000,
        )

        reported_dpr = page.evaluate("() => window.devicePixelRatio")
        assert reported_dpr == dpr, (variant, reported_dpr)
        _print_pass(variant, "browser DPR")
        _assert_make_clip_add_keeps_exact_word_indexes(page, variant)

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
        _print_pass(variant, "title first-paint aspect matches viewBox")
        _assert_server_geometry_round_trip(page, variant, "title")
        _assert_server_geometry_round_trip(page, variant, "caption")

        nav_heights = page.eval_on_selector_all(
            ".generate-v2-editor-rail-btn",
            "elements => elements.map(element => element.getBoundingClientRect().height)",
        )
        assert nav_heights and max(nav_heights) <= 80, {"variant": variant, "heights": nav_heights}
        _print_pass(variant, "design nav item height <= 80px")

        _select_style(page, "electric_lime")
        effect = _caption_effect(page)
        assert "#C6F24D" in effect["stops"], effect
        _print_pass(variant, "Electric Lime resolves to the scoped lime gradient")
        _select_style(page, "blue_pop")
        effect = _caption_effect(page)
        assert "#2E7FFF" in effect["rects"], effect
        _print_pass(variant, "Blue Pop resolves to the blue active-word pill")
        _select_style(page, "silver_pop")
        effect = _caption_effect(page)
        assert "#FFFFFF" in effect["stops"] and "#B5BBC4" in effect["stops"], effect
        _print_pass(variant, "Silver Pop resolves to the scoped silver gradient")

        before_height = page.evaluate(
            """() => ['shorts-preview-subtitle-band', 'orig-preview-caption']
              .map(id => document.getElementById(id)).find(el => el?.dataset.captionDomOwner === 'measured-svg'
                && getComputedStyle(el).display !== 'none').getBoundingClientRect().height"""
        )
        page.locator("[name='sub_font_size']").evaluate(
            """field => { field.value = String(Number(field.value || 40) + 8);
              field.dispatchEvent(new Event('input', {bubbles: true}));
              field.dispatchEvent(new Event('change', {bubbles: true})); }"""
        )
        page.wait_for_function(
            """oldHeight => { const band = ['shorts-preview-subtitle-band', 'orig-preview-caption']
                .map(id => document.getElementById(id)).find(el => el?.dataset.captionDomOwner === 'measured-svg'
                  && getComputedStyle(el).display !== 'none');
              return band && Math.abs(band.getBoundingClientRect().height - oldHeight) > 1; }""",
            arg=before_height,
        )
        _print_pass(variant, "caption font-size change updates measured band height")

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
        _print_pass(variant, "title drag stays committed and caption remains highlighted")

        duplicates = page.evaluate(
            """() => { const seen = new Set(); const duplicates = [];
              document.querySelectorAll('[id]').forEach(el => {
                if (seen.has(el.id)) duplicates.push(el.id); else seen.add(el.id); });
              return [...new Set(duplicates)]; }"""
        )
        assert not duplicates, duplicates
        _print_pass(variant, "page contains no duplicate DOM ids")
        duplicate_clip_plans = page.evaluate(
            """() => { const seen = new Set(); const duplicates = [];
              document.querySelectorAll('.clip-card[data-plan-index]').forEach(card => {
                const plan = String(card.dataset.planIndex || '').trim();
                if (!plan) return;
                if (seen.has(plan)) duplicates.push(plan); else seen.add(plan); });
              return [...new Set(duplicates)]; }"""
        )
        assert not duplicate_clip_plans, duplicate_clip_plans
        _print_pass(variant, "page contains one clip-card node per plan")
        if width == 1280 and height == 720 and dpr == 1:
            _assert_stubbed_regenerate(page, variant)
        print(f"VARIANT_OK {variant}")
    finally:
        context.close()


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
        try:
            for width, height in VIEWPORTS:
                for dpr in DPRS:
                    _run_variant(browser, base_url=base_url, cookie_name=cookie.key,
                                 cookie_value=cookie.value, selected_video=selected_video,
                                 width=width, height=height, dpr=dpr)
        finally:
            browser.close()
    print(f"SMOKE_OK video={selected_video} user={user_id} brand={brand_id} variants={len(VIEWPORTS) * len(DPRS)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", type=int, help="Transcript-ready video owned by an admin user")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    run(args.video_id, args.headed)


if __name__ == "__main__":
    main()
