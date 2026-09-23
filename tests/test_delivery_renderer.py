from autoqq_business_plugin.delivery_renderer import render_delivery_text


def message(text: str, items=None):
    data = {} if items is None else {"subscription_match": {"items": items}}
    return {"text": text, "data": data}


def test_highlights_every_exact_matching_bullet_line() -> None:
    text = (
        "【帐篷 A】\n"
        "• 搜索并救援\n"
        "• 捕获 Grineer 特工\n\n"
        "【Konzu T5】\n"
        "• 搜索并救援\n"
        "• 找出遗失的器物"
    )
    items = [
        {"key": "RescueBountyResc", "label": "搜索并救援"},
        {"key": "ReclamationBountyCap", "label": "找出遗失的器物"},
    ]

    rendered = render_delivery_text(message(text, items), max_chars=2000)

    assert rendered == (
        "【帐篷 A】\n"
        "• **搜索并救援** 🔴\n"
        "• 捕获 Grineer 特工\n\n"
        "【Konzu T5】\n"
        "• **搜索并救援** 🔴\n"
        "• **找出遗失的器物** 🔴"
    )


def test_does_not_fuzzy_match_similar_labels_or_non_bullet_text() -> None:
    text = "搜索并救援\n• 搜索并救援奖励\n• 搜索并救援"
    items = [{"key": "RescueBountyResc", "label": "搜索并救援"}]

    assert render_delivery_text(message(text, items), max_chars=2000) == (
        "搜索并救援\n• 搜索并救援奖励\n• **搜索并救援** 🔴"
    )


def test_missing_or_malformed_metadata_leaves_text_unchanged() -> None:
    text = "• 搜索并救援"
    malformed = {
        "text": text,
        "data": {"subscription_match": {"items": [{"key": "missing-label"}]}},
    }

    assert render_delivery_text(message(text), max_chars=2000) == text
    assert render_delivery_text(malformed, max_chars=2000) == text


def test_rendering_is_idempotent_and_preserves_line_endings() -> None:
    text = "【帐篷 A】\r\n• 搜索并救援\r\n"
    items = [{"key": "RescueBountyResc", "label": "搜索并救援"}]
    rendered = render_delivery_text(message(text, items), max_chars=2000)

    assert rendered == "【帐篷 A】\r\n• **搜索并救援** 🔴\r\n"
    assert render_delivery_text(message(rendered, items), max_chars=2000) == rendered


def test_escapes_markdown_in_a_catalog_label() -> None:
    text = "• target_*"
    items = [{"key": "target", "label": "target_*"}]

    assert render_delivery_text(message(text, items), max_chars=2000) == ("• **target\\_\\*** 🔴")
