"""
Shared HTML-structure checking helpers for the 'html' tier, stdlib-only
(html.parser) so no headless browser or extra pip dependency is required.

Deliberately checks STRUCTURE via required data-* attribute hooks, not
visual rendering or exact class names/styling -- see benchmark/README.md's
Evaluation methodology section for why: it keeps the check deterministic
regardless of how the model chose to style the page, the same reason real
test suites use data-testid hooks instead of CSS selectors.
"""
from html.parser import HTMLParser


class Node:
    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.text = ""


class TreeBuilder(HTMLParser):
    """Builds a lightweight DOM tree (self-closing tags handled loosely --
    good enough for structural counting, not a spec-compliant HTML parser)."""

    VOID_TAGS = {"img", "input", "br", "hr", "link", "meta", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].text += data


def parse(html_text: str) -> Node:
    builder = TreeBuilder()
    builder.feed(html_text)
    return builder.root


def find_all(node: Node, tag: str = None, attr: str = None) -> list:
    """All descendants matching an optional tag name and/or the presence of
    an attribute (any value)."""
    results = []
    for child in node.children:
        matches = (tag is None or child.tag == tag) and (attr is None or attr in child.attrs)
        if matches:
            results.append(child)
        results.extend(find_all(child, tag, attr))
    return results


def text_of(node: Node) -> str:
    return node.text + "".join(text_of(c) for c in node.children)
