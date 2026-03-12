from __future__ import annotations

ROLE_SELECTOR_MAP = {
    "button": 'button, [role="button"], input[type="button"], input[type="submit"]',
    "link": 'a[href], [role="link"]',
    "heading": 'h1, h2, h3, h4, h5, h6, [role="heading"]',
    "textbox": (
        'input:not([type]), input[type="text"], input[type="email"],'
        ' input[type="password"], input[type="search"], input[type="url"],'
        ' textarea, [role="textbox"]'
    ),
    "checkbox": 'input[type="checkbox"], [role="checkbox"]',
    "radio": 'input[type="radio"], [role="radio"]',
    "listitem": 'li, [role="listitem"]',
    "img": 'img, [role="img"]',
    "navigation": 'nav, [role="navigation"]',
}


class Locator:
    """Playwright-like locator for static HTML and live bridge elements."""

    def __init__(self, nodes: list[object]) -> None:
        self._nodes = nodes

    def filter(self, *, has_text: str | None = None) -> Locator:
        result = self._nodes
        if has_text is not None:
            target = has_text.lower()
            result = []
            for n in self._nodes:
                text = ""
                if hasattr(n, "text"):
                    text = n.text() or "" if callable(n.text) else n.text or ""
                elif hasattr(n, "_text"):
                    text = n._text or ""
                if target in text.lower():
                    result.append(n)
        return Locator(result)

    @property
    def first(self) -> object | None:
        return self._nodes[0] if self._nodes else None

    @property
    def last(self) -> object | None:
        return self._nodes[-1] if self._nodes else None

    def nth(self, index: int) -> object | None:
        if 0 <= index < len(self._nodes):
            return self._nodes[index]
        return None

    def all(self) -> list[object]:
        return list(self._nodes)

    def count(self) -> int:
        return len(self._nodes)

    def inner_text(self) -> str | None:
        node = self.first
        if node is None:
            return None
        if hasattr(node, "text") and callable(node.text):
            return node.text(deep=True)
        if hasattr(node, "text"):
            return node.text
        return None

    def get_attribute(self, name: str) -> str | None:
        node = self.first
        if node is None:
            return None
        if hasattr(node, "attrs"):
            attrs = node.attrs
            if isinstance(attrs, dict):
                return attrs.get(name)
            return attrs.get(name) if hasattr(attrs, "get") else None
        return None

    def __repr__(self) -> str:
        return f"Locator(count={self.count()})"

    def __bool__(self) -> bool:
        return len(self._nodes) > 0


class PageParser:
    """Parses a static HTML snapshot and provides Playwright-style query methods.

    Uses selectolax if available, otherwise falls back to basic regex parsing.
    """

    def __init__(self, html: str) -> None:
        self._html = html
        self._tree = None
        try:
            from selectolax.lexbor import LexborHTMLParser

            self._tree = LexborHTMLParser(html)
        except ImportError:
            pass

    @property
    def title(self) -> str | None:
        if self._tree:
            node = self._tree.css_first("title")
            return node.text() if node else None
        import re

        m = re.search(
            r"<title[^>]*>(.*?)</title>", self._html, re.IGNORECASE | re.DOTALL
        )
        return m.group(1).strip() if m else None

    def locator(self, css: str, *, has_text: str | None = None) -> Locator:
        if self._tree:
            nodes = list(self._tree.css(css))
            loc = Locator(nodes)
            if has_text is not None:
                loc = loc.filter(has_text=has_text)
            return loc
        return Locator([])

    @staticmethod
    def _css_escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )

    def get_by_text(self, text: str, *, exact: bool = False) -> Locator:
        if not self._tree:
            return Locator([])
        safe = self._css_escape(text)
        candidates = list(self._tree.css(f':lexbor-contains("{safe}" i)'))
        if exact:
            candidates = [n for n in candidates if (n.text(strip=True) or "") == text]
        return Locator(candidates)

    def get_by_role(self, role: str, *, name: str | None = None) -> Locator:
        if not self._tree:
            return Locator([])
        selector = ROLE_SELECTOR_MAP.get(role, f'[role="{role}"]')
        nodes = list(self._tree.css(selector))
        if name is not None:
            target = name.lower()
            nodes = [
                n
                for n in nodes
                if target in (n.text(strip=True) or "").lower()
                or target in (n.attrs.get("aria-label", "") or "").lower()
                or target in (n.attrs.get("title", "") or "").lower()
                or target in (n.attrs.get("value", "") or "").lower()
            ]
        return Locator(nodes)

    def get_by_placeholder(self, text: str, *, exact: bool = False) -> Locator:
        if not self._tree:
            return Locator([])
        if exact:
            nodes = list(self._tree.css(f'[placeholder="{self._css_escape(text)}"]'))
        else:
            target = text.lower()
            all_inputs = self._tree.css("[placeholder]")
            nodes = [
                n
                for n in all_inputs
                if target in (n.attrs.get("placeholder", "") or "").lower()
            ]
        return Locator(nodes)

    def get_by_label(self, text: str) -> Locator:
        if not self._tree:
            return Locator([])
        target = text.lower()
        labels = self._tree.css("label")
        results = []
        for label in labels:
            if target not in (label.text(strip=True) or "").lower():
                continue
            for_attr = label.attrs.get("for")
            if for_attr:
                el = self._tree.css_first(f"#{for_attr}")
                if el:
                    results.append(el)
            else:
                inp = label.css_first("input, textarea, select")
                if inp:
                    results.append(inp)
        return Locator(results)

    def get_by_test_id(self, test_id: str) -> Locator:
        if not self._tree:
            return Locator([])
        nodes = list(self._tree.css(f'[data-testid="{self._css_escape(test_id)}"]'))
        return Locator(nodes)
