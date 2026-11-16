from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Any


@dataclass(frozen=True)
class SoldResultsRow:
    title: str
    sold_price: float | None
    postage: float | None
    sold_date: str
    best_offer: bool
    listing_url: str
    raw_price_text: str
    raw_shipping_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "soldPrice": self.sold_price,
            "postage": self.postage,
            "soldDate": self.sold_date,
            "bestOffer": self.best_offer,
            "listingUrl": self.listing_url,
        }


class _EbaySoldResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_item = False
        self._in_title = False
        self._in_price = False
        self._in_caption = False
        self._in_shipping = False
        self._current_href = ""
        self._title_parts: list[str] = []
        self._price_text = ""
        self._caption_text = ""
        self._shipping_text = ""
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        klass = attr.get("class", "")
        if tag == "li" and "s-item" in klass.split():
            self._in_item = True
            self._reset_item()
        if not self._in_item:
            return
        if tag == "a" and "s-item__link" in klass.split():
            self._current_href = attr.get("href", "")
        if tag in {"h3", "span"} and "s-item__title" in klass.split():
            self._in_title = True
        if tag == "span" and "s-item__price" in klass.split():
            self._in_price = True
        if tag == "span" and "s-item__caption" in klass.split():
            self._in_caption = True
        if tag == "span" and "s-item__shipping" in klass.split():
            self._in_shipping = True

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag in {"h3", "span"}:
            self._in_title = False
        if self._in_price and tag == "span":
            self._in_price = False
        if self._in_caption and tag == "span":
            self._in_caption = False
        if self._in_shipping and tag == "span":
            self._in_shipping = False
        if tag == "li" and self._in_item:
            title = " ".join(self._title_parts).strip()
            if title:
                self.rows.append(
                    {
                        "title": title,
                        "href": self._current_href,
                        "price": self._price_text.strip(),
                        "caption": self._caption_text.strip(),
                        "shipping": self._shipping_text.strip(),
                    }
                )
            self._in_item = False

    def handle_data(self, data: str) -> None:
        if not self._in_item:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
        elif self._in_price:
            self._price_text += (" " if self._price_text else "") + text
        elif self._in_caption:
            self._caption_text += (" " if self._caption_text else "") + text
        elif self._in_shipping:
            self._shipping_text += (" " if self._shipping_text else "") + text

    def _reset_item(self) -> None:
        self._current_href = ""
        self._title_parts = []
        self._price_text = ""
        self._caption_text = ""
        self._shipping_text = ""


def _parse_money(text: str) -> float | None:
    if not text:
        return None
    match = re.search(r"[\$]?\s*([\d,]+(?:\.\d{1,2})?)", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_sold_date(caption: str, title: str) -> str:
    blob = f"{caption} {title}"
    match = re.search(r"Sold\s+(\d{1,2}\s+\w+\s+\d{4})", blob, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return caption.strip()


def parse_sold_results_html(html: str) -> list[SoldResultsRow]:
    """Parse every sold row from saved eBay AU HTML (fixture / replay; no live fetch)."""
    parser = _EbaySoldResultsParser()
    parser.feed(html)
    results: list[SoldResultsRow] = []
    for raw in parser.rows:
        title = raw["title"]
        caption = raw.get("caption") or ""
        combined = f"{title} {caption}".lower()
        results.append(
            SoldResultsRow(
                title=title,
                sold_price=_parse_money(raw.get("price") or ""),
                postage=_parse_money(raw.get("shipping") or ""),
                sold_date=_parse_sold_date(caption, title),
                best_offer="best offer" in combined,
                listing_url=raw.get("href") or "",
                raw_price_text=raw.get("price") or "",
                raw_shipping_text=raw.get("shipping") or "",
            )
        )
    return results