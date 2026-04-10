"""
Amazon scraper for packaging dimensions.
Uses Playwright (headless Chromium) to handle bot detection.
Supports multiple marketplaces: de, fr, nl, it.
"""
import asyncio
import re
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Match codes like BT7660, BT7660/15, QP2630/30, S9000, MG7720
MODEL_PATTERN = r'\b([A-Z]{1,3}\d{3,5}(?:/\d{2})?)\b'

# Marketplace TLD mapping
MARKETPLACE_TLDS = {
    "de": "de",
    "fr": "fr",
    "nl": "nl",
    "it": "it",
}

def extract_model_codes(text: str) -> list[str]:
    """Extract model codes. Returns canonical (with /xx suffix) where present."""
    codes = set()
    for m in re.finditer(MODEL_PATTERN, text):
        code = m.group(1).upper()
        # Keep only codes with at least 1 letter + 3-5 digits
        prefix_match = re.match(r'^([A-Z]+)(\d{3,5})(/\d{2})?$', code)
        if prefix_match:
            codes.add(code)
    return sorted(codes)


def short_code(code: str) -> str:
    """BT7660/15 → BT7660"""
    return code.split("/")[0]


def parse_family_filter(query: str) -> str | None:
    """
    Parse user query to extract a model family prefix.
    "Philips BT 7000 NEW"   → "BT7"
    "Philips Shaver S7000"  → "S7"
    "OneBlade QP2630"       → "QP2"
    Returns None if no clear family found.
    """
    # Match: optional prefix letters, optional space, then a digit
    # Prefer patterns with explicit letter prefix
    m = re.search(r'\b([A-Z]{1,3})\s*(\d)\d{0,3}\b', query.upper())
    if m:
        return m.group(1) + m.group(2)
    return None


def extract_year(text: str) -> int:
    """Extract a 4-digit year from text (2018-2030 range), 0 if none."""
    m = re.search(r'\b(20[1-3]\d)\b', text)
    return int(m.group(1)) if m else 0


def infer_related_models(model_code: str) -> list[str]:
    """
    Given a model code like BT7660, generate sibling candidates:
    - Same prefix, numerically adjacent variants (±10, ±50, ±100 steps)
    - e.g. BT7660 → BT7650, BT7640, BT7710, BT7720
    """
    m = re.match(r'^([A-Z]+)(\d+)$', model_code)
    if not m:
        return []
    prefix, num_str = m.group(1), m.group(2)
    num = int(num_str)
    width = len(num_str)

    candidates = set()
    for delta in [-100, -50, -20, -10, 10, 20, 50, 100]:
        candidate = num + delta
        if candidate > 0:
            candidates.add(f"{prefix}{str(candidate).zfill(width)}")
    return sorted(candidates)


async def amazon_search(page, query: str, marketplace: str = "de") -> list[dict]:
    """Search Amazon and return top results with title, ASIN, URL."""
    tld = MARKETPLACE_TLDS.get(marketplace, "de")
    base_url = f"https://www.amazon.{tld}"
    url = f"{base_url}/s?k={query.replace(' ', '+')}"
    await page.goto(url, timeout=30000)
    await page.wait_for_timeout(2000)

    results = []
    items = await page.query_selector_all('div.s-result-item[data-asin]')
    for item in items[:25]:
        asin = await item.get_attribute("data-asin")
        if not asin or len(asin) < 5:
            continue
        title_el = await item.query_selector("h2 span, h2 a span")
        title = (await title_el.inner_text()).strip() if title_el else ""
        if not title:
            continue
        results.append({
            "asin": asin,
            "title": title,
            "url": f"{base_url}/dp/{asin}",
        })
    return results


async def fetch_product_details(page, asin: str, marketplace: str = "de") -> dict:
    """
    Fetch a product page and extract:
    - Full title
    - Model code(s) found in title / bullet points
    - Packaging dimensions (from Produktinformationen table)
    - Package weight
    - Raw bullet points (for materials text)
    - Price, main image URL, EAN/GTIN, included components
    """
    tld = MARKETPLACE_TLDS.get(marketplace, "de")
    base_url = f"https://www.amazon.{tld}"
    url = f"{base_url}/dp/{asin}"
    try:
        await page.goto(url, timeout=30000)
        await page.wait_for_timeout(1500)
    except PWTimeout:
        return {"asin": asin, "error": "Timeout loading page"}

    # ---- Title ----
    title = ""
    for sel in ["#productTitle", "#title span"]:
        el = await page.query_selector(sel)
        if el:
            title = (await el.inner_text()).strip()
            break

    # ---- Bullet points ----
    bullets = []
    for el in await page.query_selector_all("#feature-bullets li span"):
        text = (await el.inner_text()).strip()
        if text:
            bullets.append(text)

    # ---- Collect product info from EVERY known location on the page ----
    # Amazon.de spreads structured info across several blocks. We harvest all of them.
    tech_details: dict[str, str] = {}

    # 1. Tech-spec tables (left/right columns) and the prodDetails block
    table_selectors = [
        "#productDetails_techSpec_section_1 tr",
        "#productDetails_techSpec_section_2 tr",
        "#productDetails_detailBullets_sections1 tr",
        "#productDetails_db_sections tr",
        "#prodDetails tr",
        ".prodDetTable tr",
        "table.a-keyvalue tr",
    ]
    for sel in table_selectors:
        for row in await page.query_selector_all(sel):
            th = await row.query_selector("th")
            td = await row.query_selector("td")
            if th and td:
                key = (await th.inner_text()).strip().rstrip(":").strip()
                val = (await td.inner_text()).strip()
                # Clean trailing whitespace, collapse internal whitespace
                val = re.sub(r"\s+", " ", val)
                if key and val and key not in tech_details:
                    tech_details[key] = val

    # 2. Detail bullets list — Amazon.de's most common location for dimensions/weight
    # Format: <span class="a-text-bold">Produktabmessungen ‏ : ‎</span><span>4 x 6 x 17 cm; 200 g</span>
    bullet_items = await page.query_selector_all(
        "#detailBullets_feature_div li, "
        "#detailBulletsWrapper_feature_div li, "
        ".detail-bullet-list li"
    )
    for li in bullet_items:
        spans = await li.query_selector_all("span span")
        if len(spans) >= 2:
            key = (await spans[0].inner_text()).strip()
            val = (await spans[1].inner_text()).strip()
            # Strip the unicode separator chars Amazon uses (‏ ‎ : etc.)
            key = re.sub(r"[\u200e\u200f:‏‎]+", "", key).strip().rstrip(":").strip()
            val = re.sub(r"\s+", " ", val).strip()
            if key and val and key not in tech_details:
                tech_details[key] = val

    # ---- Extract packaging dimensions / weight ----
    pkg_dims = ""
    pkg_weight = ""
    pkg_volume = ""

    dim_keys = ["Abmessungen", "Produktabmessungen", "Verpackungsabmessungen", "Package Dimensions",
                "Paketabmessungen", "Artikelabmessungen", "Größe"]
    weight_keys = ["Gewicht", "Produktgewicht", "Versandgewicht", "Item Weight", "Paketgewicht"]
    volume_keys = ["Volumen", "Paketvolumen"]

    for k, v in tech_details.items():
        kl = k.lower()
        if any(d.lower() in kl for d in dim_keys) and not pkg_dims:
            pkg_dims = v
        elif any(w.lower() in kl for w in weight_keys) and not pkg_weight:
            pkg_weight = v
        elif any(vol.lower() in kl for vol in volume_keys) and not pkg_volume:
            pkg_volume = v

    # Amazon.de often combines dims + weight in one field, e.g.:
    # "21,4 x 13,9 x 6,4 cm; 464 Gramm"
    # Split on the semicolon and classify each part.
    if pkg_dims and ";" in pkg_dims:
        parts = [p.strip() for p in pkg_dims.split(";")]
        dim_part = ""
        weight_part = ""
        for part in parts:
            # Dimension parts contain "x" or "×" and a length unit
            if re.search(r'\d\s*[x×]\s*\d', part, re.IGNORECASE) and \
               re.search(r'\b(cm|mm|m|inch|in)\b', part, re.IGNORECASE):
                if not dim_part:
                    dim_part = part
            # Weight parts: number + weight unit (longer unit names listed first)
            elif re.search(
                r'\d\s*(kilogramm|kilogram|gramm|gram|pound|ounce|kg|lb|oz|g)\b',
                part, re.IGNORECASE,
            ):
                if not weight_part:
                    weight_part = part
        if dim_part:
            pkg_dims = dim_part
        if weight_part and not pkg_weight:
            pkg_weight = weight_part

    # ---- Model codes ----
    # The most reliable source is the structured "Modellnummer" field.
    # Fall back to scanning title + bullets for codes matching the regex.
    model_number_field = ""
    for k, v in tech_details.items():
        if "modellnummer" in k.lower() or "model number" in k.lower():
            model_number_field = v.strip()
            break

    model_codes = []
    if model_number_field:
        # Extract just the code portion (e.g. "BT7660/15" from "BT7660/15")
        codes_found = extract_model_codes(model_number_field)
        if codes_found:
            model_codes = codes_found
        else:
            model_codes = [model_number_field]

    if not model_codes:
        full_text = title + " " + " ".join(bullets)
        model_codes = extract_model_codes(full_text)[:3]

    # ---- Price ----
    price = ""
    for sel in [
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
        "span.a-price span.a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
    ]:
        el = await page.query_selector(sel)
        if el:
            price = (await el.inner_text()).strip()
            if price:
                break

    # ---- Main product image URL ----
    image_url = ""
    for sel in ["#landingImage", "#imgBlkFront", "#main-image"]:
        el = await page.query_selector(sel)
        if el:
            image_url = (await el.get_attribute("src")) or ""
            if image_url:
                break

    # ---- EAN / GTIN ----
    ean_gtin = ""
    ean_keys = ["EAN", "GTIN", "Global Trade Identification Number"]
    for k, v in tech_details.items():
        if any(ek.lower() == k.lower() for ek in ean_keys):
            ean_gtin = v
            break

    # ---- Included Components ----
    components = ""
    comp_keys = ["Eingeschlossene Komponenten", "Included Components",
                 "Composants inclus", "Componenti inclusi", "Meegeleverde componenten"]
    for k, v in tech_details.items():
        if any(ck.lower() == k.lower() for ck in comp_keys):
            components = v
            break

    # ---- Deep link to product details section ----
    details_url = f"{base_url}/dp/{asin}#productDetails"

    return {
        "asin": asin,
        "title": title,
        "url": f"{base_url}/dp/{asin}",
        "details_url": details_url,
        "model_codes": model_codes,
        "pkg_dims": pkg_dims,
        "pkg_weight": pkg_weight,
        "pkg_volume": pkg_volume,
        "price": price,
        "image_url": image_url,
        "ean_gtin": ean_gtin,
        "components": components,
        "bullets": bullets,
        "tech_details": tech_details,
    }


async def run_search(query: str, max_variants: int = 3, marketplace: str = "de") -> list[dict]:
    """
    Main entry point.
    1. Parse the query for a model family prefix (e.g. "BT 7000" -> "BT7").
    2. Search Amazon for the query on the specified marketplace.
    3. Filter result titles to those containing a model code matching the family.
    4. If we don't have enough, do an additional search using the family code.
    5. Fetch product details for each candidate ASIN, sorted by model number desc.
    """
    family = parse_family_filter(query)

    # If user mentions a known brand, require it to appear in result titles.
    KNOWN_BRANDS = ["philips", "braun", "oral-b", "oral b", "gillette", "remington", "panasonic"]
    brand_filter = None
    qlow = query.lower()
    for b in KNOWN_BRANDS:
        if b in qlow:
            brand_filter = b.replace("oral b", "oral-b")
            break

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # candidates: model_code (without /xx suffix) -> asin
        candidates: dict[str, str] = {}

        async def gather(search_query: str):
            results = await amazon_search(page, search_query, marketplace=marketplace)
            for r in results:
                # Brand filter: title must contain the user's brand if specified
                if brand_filter and brand_filter not in r["title"].lower():
                    continue
                codes = extract_model_codes(r["title"])
                for full_code in codes:
                    base = short_code(full_code)
                    # Apply family filter: code must start with family prefix
                    if family and not base.upper().startswith(family):
                        continue
                    if base not in candidates:
                        candidates[base] = r["asin"]

        # Pass 1: original query
        await gather(query)

        # Pass 2: if not enough matches, search by family prefix directly
        if family and len([c for c in candidates if c.startswith(family)]) < max_variants:
            await gather(f"Philips {family}")

        # Sort: numeric portion descending (higher = newer within a series)
        def sort_key(code):
            m = re.match(r'^([A-Z]+)(\d+)$', code)
            return -int(m.group(2)) if m else 0

        sorted_codes = sorted(candidates.keys(), key=sort_key)

        # Fetch details for top distinct ASINs
        seen_asins = set()
        rows = []
        for code in sorted_codes:
            asin = candidates[code]
            if asin in seen_asins:
                continue
            seen_asins.add(asin)
            details = await fetch_product_details(page, asin, marketplace=marketplace)
            rows.append(details)
            if len(rows) >= max_variants:
                break

        await browser.close()
        return rows


if __name__ == "__main__":
    import json
    results = asyncio.run(run_search("Philips Beard Trimmer 7000", max_variants=3, marketplace="de"))
    print(json.dumps(results, indent=2, ensure_ascii=False))
