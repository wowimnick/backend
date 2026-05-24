"""Tests for Gemini description JSON parsing repairs."""

from __future__ import annotations

from quickstart.utils.description_formatter import _parse_description_formatter_json

# Invalid JSON: literal newlines inside "body" strings (common Gemini output).
CHOCOLATE_TRUFFLE_LIKE_JSON = """
{
  "summary": "Ever dreamed of making your own gourmet chocolate truffles?",
  "sections": [
    {
      "title": "✨ Your Chocolate Adventure Awaits!",
      "body": "Get ready to unleash your inner chocolatier!"
    },
    {
      "title": "💡 What You'll Master",
      "body": "You'll uncover the sweet secrets behind crafting perfect truffles, including:
-   **Tempering chocolate** like a pro
-   **Hand-rolling** your truffles"
    }
  ]
}
"""


class TestDescriptionFormatterJsonParse:
    def test_repairs_literal_newlines_inside_string_values(self):
        parsed = _parse_description_formatter_json(CHOCOLATE_TRUFFLE_LIKE_JSON)
        assert parsed is not None
        assert "summary" in parsed
        sections = parsed.get("sections") or []
        assert len(sections) >= 2
        body = sections[1].get("body") or ""
        assert "Tempering chocolate" in body
        assert "-   **Tempering chocolate**" in body

    def test_valid_json_unchanged(self):
        raw = '{"summary": "Hi", "sections": [{"title": "📖 A", "body": "Line one\\n\\nLine two"}]}'
        parsed = _parse_description_formatter_json(raw)
        assert parsed["summary"] == "Hi"
        assert parsed["sections"][0]["body"] == "Line one\n\nLine two"
