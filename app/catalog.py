"""Product catalog: loading and fuzzy search."""
import difflib
import json
import re
from typing import Optional

SYNONYMS = {
    "kurti": "kurta", "sari": "saree", "earring": "jhumka", "earrings": "jhumka",
    "chappal": "chappals", "sandals": "chappals", "shoes": "footwear", "bag": "potli",
    "necklace": "necklace", "jacket": "jacket", "suit": "pathani", "lal": "red",
    "neela": "navy", "safed": "white", "kala": "black", "peela": "yellow",
}


class Catalog:
    def __init__(self, products: list[dict]):
        self.products = products
        self.by_sku = {p["sku"].upper(): p for p in products}

    @classmethod
    def load(cls, path: str) -> "Catalog":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def get(self, sku: str) -> Optional[dict]:
        return self.by_sku.get((sku or "").upper())

    def search(self, query: str = "", max_price: Optional[float] = None,
               category: Optional[str] = None, size: Optional[str] = None,
               color: Optional[str] = None, limit: int = 5) -> list[dict]:
        # category and color are soft filters: they boost matches instead of excluding items,
        # so an LLM guessing "earrings" or "silver" still finds jewellery.
        text = " ".join(filter(None, [query, category, color])).lower()
        words = [SYNONYMS.get(w, w) for w in re.findall(r"[a-z0-9]+", text)]
        scored = []
        for p in self.products:
            if p["stock"] <= 0:
                continue
            if max_price is not None and p["price"] > max_price:
                continue
            if size and size.upper() not in [s.upper() for s in p["sizes"]] and "Free" not in p["sizes"]:
                continue
            hay = f'{p["name"]} {p["category"]} {p["color"]} {p["description"]}'.lower()
            tokens = re.findall(r"[a-z0-9]+", hay)
            score = 0.0
            for w in words:
                if w in hay:
                    score += 2
                elif difflib.get_close_matches(w, tokens, n=1, cutoff=0.8):
                    score += 1
            if words and score < 2 and not (score and len(words) == 1):
                continue
            scored.append((score, -p["price"], p))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [p for _, _, p in scored[:limit]]
