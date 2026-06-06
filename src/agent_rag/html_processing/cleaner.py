"""HTML cleaning inspired by HtmlRAG: strip scripts, styles, comments, and redundant nesting."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser, Node


_REMOVE_TAGS = {
    "script", "style", "noscript", "svg", "iframe", "object", "embed",
    "applet", "link", "meta",
}

_STRIP_ATTRS_KEEP = {"href", "src", "alt", "id", "class"}


def clean_html(raw_html: str) -> str:
    """Return a cleaned HTML string with only semantic content tags preserved."""
    tree = HTMLParser(raw_html)

    for node in tree.css(",".join(_REMOVE_TAGS)):
        node.decompose()

    for node in tree.root.traverse():  # type: ignore[union-attr]
        if not isinstance(node, Node):
            continue
        if node.tag == "-text":
            continue
        if node.tag == "-comment":
            node.decompose()
            continue
        attrs_to_remove = []
        for attr_name in (node.attributes or {}):
            if attr_name not in _STRIP_ATTRS_KEEP:
                attrs_to_remove.append(attr_name)
        for attr_name in attrs_to_remove:
            try:
                node.attrs.pop(attr_name, None)  # type: ignore[union-attr]
            except Exception:
                pass

    html_out = tree.html or ""
    html_out = re.sub(r"<!--.*?-->", "", html_out, flags=re.DOTALL)
    html_out = re.sub(r"\n\s*\n+", "\n", html_out)
    html_out = re.sub(r"[ \t]+", " ", html_out)
    return html_out.strip()
