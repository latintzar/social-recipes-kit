#!/usr/bin/env python3
"""One-shot migration: normalise demo seed JSONs to the 10-bucket ontology.

What this does
--------------
1. Reads ``products.json`` + ``recipe_products.json``.
2. Maps every legacy ``category`` to ``{category, subcategory, cuisine?}``
   per the ten top-level buckets in ``ONTOLOGY.md``.
3. Title-cases ``name_en`` / ``name_bg`` (the unit suffix stays lowercase
   — "500g" not "500G").
4. Appends ~30 staple SKUs that recipe matching kept tripping on
   (cabbage, cucumber, herbs, rice, olive oil, tortilla wraps, tofu, …).
5. Writes the JSONs back in-place.

Idempotent — running it twice on already-migrated JSONs is a no-op
(detected via the presence of ``subcategory``).

Run from anywhere::

    python apps/vitrina/product/backend/mcp/seed/migrate_ontology.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRODUCTS = HERE / "products.json"
RECIPE_PRODUCTS = HERE / "recipe_products.json"


# ── ontology mapping ─────────────────────────────────────────────────────


# Legacy category → (top, subcategory, cuisine?)
CATEGORY_MAP: dict[str, tuple[str, str, list[str] | None]] = {
    # cheese family — collapse 5 cheese-* buckets into one top with subtypes
    "cheese-yellow": ("cheese", "yellow", None),
    "cheese-white": ("cheese", "white", None),
    "cheese-fresh": ("cheese", "fresh", None),
    "cheese-hard": ("cheese", "hard", None),
    "cheese-spread": ("cheese", "spread", None),
    # dairy
    "yogurt": ("dairy", "yogurt", None),
    "milk": ("dairy", "milk", None),
    "butter": ("dairy", "butter", None),
    "cream": ("dairy", "cream", None),
    "eggs": ("dairy", "eggs", None),
    "drink": ("beverage", "soft", None),
    # meat
    "fresh-meat": ("meat", "fresh", None),
    "cured-meat": ("meat", "cured", None),
    "cured-sausage": ("meat", "sausage", None),
    "bbq": ("meat", "bbq", None),
    # bakery
    "bread": ("bakery", "bread", None),
    "bread-buns": ("bakery", "bun", None),
    "dough": ("bakery", "dough", None),
    # produce
    "vegetables": ("produce", "vegetable", None),
    "greens": ("produce", "leafy", None),
    "fruit": ("produce", "fruit", None),
    # pantry
    "pasta": ("pantry", "pasta", None),
    "oil": ("pantry", "oil", None),
    "vinegar": ("pantry", "vinegar", None),
    "spice": ("pantry", "spice", None),
    "baking": ("pantry", "baking", None),
    # canned + condiment
    "canned": ("canned", "tomato", None),
    "condiment": ("condiment", "sauce", None),
    "pickles": ("condiment", "pickle", None),
    "stock": ("canned", "stock", None),
    "pantry": ("pantry", "baking", None),
    "sweets": ("specialty", "sweet", None),
    # asian-* → standard buckets + cuisine tag
    "asian-sauce": ("condiment", "sauce", ["asian"]),
    "asian-oil": ("pantry", "oil", ["asian"]),
    "asian-pantry": ("pantry", "breadcrumbs", ["asian"]),
    # beverage
    "wine": ("beverage", "wine", None),
    # non-grocery — leave as their own top categories so they don't pollute
    # the grocery matcher universe
    "hand-tool": ("hardware", "hand-tool", None),
    "power-tool": ("hardware", "power-tool", None),
    "fastener": ("hardware", "fastener", None),
    "painting": ("hardware", "painting", None),
    "electrical": ("hardware", "electrical", None),
    "tape": ("hardware", "tape", None),
    "service": ("hardware", "service", None),
    "paint": ("hardware", "paint", None),
    "safety": ("hardware", "safety", None),
    "measuring": ("hardware", "measuring", None),
    "toilet": ("plumbing", "toilet", None),
    "sink": ("plumbing", "sink", None),
    "tap": ("plumbing", "tap", None),
    "shower": ("plumbing", "shower", None),
    "bathtub": ("plumbing", "bathtub", None),
    "mirror": ("plumbing", "mirror", None),
    "heating": ("plumbing", "heating", None),
    "tile": ("plumbing", "tile", None),
    "consumable": ("plumbing", "consumable", None),
}


# ── pretty Title Case for names ──────────────────────────────────────────


# Words we keep lowercase inside a Title Case name (English style guide).
_LOWERCASE_INNER = {
    "and", "or", "of", "the", "in", "with", "for", "a", "an", "to",
    "from", "by", "on", "at", "as",
}

# Tokens that should NEVER be touched (keep original casing).
_KEEP_AS_IS = {
    "BG", "EU", "DOP", "PDO", "IGP", "PGI", "BBQ", "EVOO",
    "USB", "LED", "PVC",
}

# Standalone unit words — lowercase them when we encounter them.
_UNIT_WORDS = {"pcs", "ml", "l", "g", "kg", "oz", "lb", "lbs", "cl"}

# Unit suffixes — if the trailing token matches, lowercase it.
_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-zA-Zг]+)$")


def _title_case_token(tok: str) -> str:
    if not tok:
        return tok
    if tok.lower() in _UNIT_WORDS:
        return tok.lower()
    if tok.upper() in _KEEP_AS_IS:
        return tok.upper()
    # numeric+unit, e.g. "500G" → "500g", "1KG" → "1kg"
    m = _UNIT_RE.match(tok)
    if m:
        return f"{m.group(1)}{m.group(2).lower()}"
    # "4бр", "500г" — Cyrillic units
    if re.match(r"^\d+\s*(?:бр|г|кг|мл|л)$", tok, re.IGNORECASE):
        return tok.lower()
    if tok[0].isalpha():
        return tok[0].upper() + tok[1:].lower()
    return tok


def title_case(name: str) -> str:
    """Title-case while keeping unit suffixes lowercase ("500g", not "500G")."""
    if not name:
        return name
    parts = name.split()
    out: list[str] = []
    for i, part in enumerate(parts):
        # tokens with internal punctuation: process each segment
        if any(c in part for c in "-/"):
            # split on boundary, retain separators
            segs = re.split(r"([-/])", part)
            cased = "".join(
                seg if seg in {"-", "/"} else _title_case_token(seg) for seg in segs
            )
            out.append(cased)
        else:
            cased = _title_case_token(part)
            # interior connectors like "and"/"of" lowercase mid-name
            if i != 0 and i != len(parts) - 1 and cased.lower() in _LOWERCASE_INNER:
                cased = cased.lower()
            out.append(cased)
    return " ".join(out)


# ── new staple SKUs (the missing-ingredients list) ───────────────────────


def _img(slug: str) -> str:
    return f"https://images.unsplash.com/photo-{slug}?w=1200&h=1200&fit=crop&q=85"


NEW_STAPLES: list[dict] = [
    # ── zelenchukov-pazar (produce) ────────────────────────────────────
    {
        "id": "ZP-CABBAGE-WHITE-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Бяло Зеле 1кг",
        "name_en": "White Cabbage 1kg",
        "description_bg": "Свежо бяло зеле — за зелева салата, сарми и кисели зели.",
        "description_en": "Fresh white cabbage — for slaw, sarmi and sauerkraut.",
        "category": "produce",
        "subcategory": "leafy",
        "keywords": [
            "cabbage", "white cabbage", "green cabbage", "savoy", "napa",
            "зеле", "бяло зеле", "slaw", "coleslaw", "sarmi",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 30,
        "image": _img("1543339494-d9b3a87e6b2b"),
        "price_eur": 0.95,
    },
    {
        "id": "ZP-CABBAGE-RED-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Червено Зеле 1кг",
        "name_en": "Red Cabbage 1kg",
        "description_bg": "Хрупкаво червено зеле — за салати, тако гарнитура, кисело зеле.",
        "description_en": "Crunchy red cabbage — for slaw, taco garnish, pickled cabbage.",
        "category": "produce", "subcategory": "leafy",
        "keywords": [
            "red cabbage", "purple cabbage", "cabbage", "червено зеле",
            "зеле", "slaw", "taco",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 20,
        "image": _img("1597393353415-b3730f3719fe"),
        "price_eur": 1.20,
    },
    {
        "id": "ZP-CUCUMBER-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Краставици 1кг",
        "name_en": "Cucumbers 1kg",
        "description_bg": "Български тънкокори краставици — за шопска, таратор, сандвичи.",
        "description_en": "Thin-skinned Bulgarian cucumbers — for shopska, tarator and sandwiches.",
        "category": "produce", "subcategory": "vegetable",
        "keywords": [
            "cucumber", "cucumbers", "english cucumber", "краставица",
            "краставици", "shopska", "tarator", "tzatziki",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 40,
        "image": _img("1604977042946-1eecc30f269e"),
        "price_eur": 1.85,
    },
    {
        "id": "ZP-PEPPER-BELL-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Чушки Капия 1кг",
        "name_en": "Bell Peppers (Kapia) 1kg",
        "description_bg": "Сладки червени капии — за лютеница, печени чушки, фахитас.",
        "description_en": "Sweet red kapia peppers — for lyutenitsa, roasted peppers, fajitas.",
        "category": "produce", "subcategory": "vegetable",
        "keywords": [
            "bell pepper", "red pepper", "capsicum", "kapia", "чушка",
            "чушки", "капия", "fajita", "lyutenitsa",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 28,
        "image": _img("1525607551316-4a8e016f126b"),
        "price_eur": 2.20,
    },
    {
        "id": "ZP-CHILI-GREEN-200",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Люти Зелени Чушки 200г",
        "name_en": "Green Chili Peppers 200g",
        "description_bg": "Свежи люти зелени чушки — за тако, ориенталски къри, салати.",
        "description_en": "Fresh green chili peppers — for tacos, curry, salsa.",
        "category": "produce", "subcategory": "vegetable",
        "keywords": [
            "chili", "chili pepper", "green chili", "jalapeno", "serrano",
            "лютa чушка", "люти чушки", "salsa",
        ],
        "unit": "pack", "unit_size": "200g", "stock": 25,
        "image": _img("1583691537756-3c50c0c52ad8"),
        "price_eur": 1.40,
    },
    {
        "id": "ZP-CARROTS-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Моркови 1кг",
        "name_en": "Carrots 1kg",
        "description_bg": "Български моркови — за супи, мирпуа, печено.",
        "description_en": "Bulgarian carrots — for soup, mirepoix and roasts.",
        "category": "produce", "subcategory": "root",
        "keywords": [
            "carrot", "carrots", "морков", "моркови", "mirepoix", "soffritto",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 35,
        "image": _img("1582515073490-39981397c445"),
        "price_eur": 0.90,
    },
    {
        "id": "ZP-MUSHROOMS-500",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Печурки 500г",
        "name_en": "Button Mushrooms 500g",
        "description_bg": "Бели печурки — за паста, ризото, омлети.",
        "description_en": "White button mushrooms — for pasta, risotto, omelettes.",
        "category": "produce", "subcategory": "mushroom",
        "keywords": [
            "mushroom", "mushrooms", "button mushroom", "champignon",
            "печурки", "гъби",
        ],
        "unit": "pack", "unit_size": "500g", "stock": 22,
        "image": _img("1607690424560-35d967d6ad7f"),
        "price_eur": 1.95,
    },
    {
        "id": "ZP-ZUCCHINI-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Тиквички 1кг",
        "name_en": "Zucchini 1kg",
        "description_bg": "Млади тиквички — за пълнене, на скара, в супи.",
        "description_en": "Young zucchini — for stuffing, grilling, soups.",
        "category": "produce", "subcategory": "vegetable",
        "keywords": [
            "zucchini", "courgette", "тиквички", "тиквичка",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 18,
        "image": _img("1566842600175-97dca489844f"),
        "price_eur": 1.60,
    },
    {
        "id": "ZP-EGGPLANT-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Патладжани 1кг",
        "name_en": "Eggplants 1kg",
        "description_bg": "Лъскави патладжани — за кьопоолу, мусака, пармиджано.",
        "description_en": "Glossy eggplants — for kyopoolu, moussaka, parmigiana.",
        "category": "produce", "subcategory": "vegetable",
        "keywords": [
            "eggplant", "aubergine", "патладжан", "патладжани", "kyopoolu",
            "moussaka", "parmigiana",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 16,
        "image": _img("1601493700631-26e89e84e6c8"),
        "price_eur": 2.10,
    },
    {
        "id": "ZP-GINGER-100",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Корен Джинджифил 100г",
        "name_en": "Ginger Root 100g",
        "description_bg": "Свеж джинджифил — за азиатски рецепти, чай, мариновки.",
        "description_en": "Fresh ginger — for Asian cooking, tea, marinades.",
        "category": "produce", "subcategory": "root",
        "keywords": [
            "ginger", "fresh ginger", "ginger root", "джинджифил",
        ],
        "unit": "pack", "unit_size": "100g", "stock": 40,
        "image": _img("1573414405881-f8c64e0e9c54"),
        "price_eur": 1.20,
    },
    {
        "id": "ZP-PARSLEY-50",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Магданоз Връзка",
        "name_en": "Parsley Bunch",
        "description_bg": "Свеж магданоз — за супи, мариновки, табуле.",
        "description_en": "Fresh flat-leaf parsley — for soup, marinades, tabbouleh.",
        "category": "produce", "subcategory": "herb",
        "keywords": [
            "parsley", "flat-leaf parsley", "italian parsley", "магданоз",
            "tabbouleh", "chimichurri",
        ],
        "unit": "bunch", "unit_size": "50g", "stock": 50,
        "image": _img("1556909114-f6e7ad7d3136"),
        "price_eur": 0.80,
    },
    {
        "id": "ZP-DILL-50",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Копър Връзка",
        "name_en": "Dill Bunch",
        "description_bg": "Свеж копър — за таратор, риба, краставици в саламура.",
        "description_en": "Fresh dill — for tarator, fish, pickled cucumbers.",
        "category": "produce", "subcategory": "herb",
        "keywords": [
            "dill", "копър", "tarator", "tzatziki",
        ],
        "unit": "bunch", "unit_size": "50g", "stock": 50,
        "image": _img("1599909533826-86b04d6df0fd"),
        "price_eur": 0.80,
    },
    {
        "id": "ZP-CILANTRO-50",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Кориандър (Кинза) Връзка",
        "name_en": "Cilantro Bunch",
        "description_bg": "Свеж кориандър — за тако, азиатски ястия, salsa.",
        "description_en": "Fresh cilantro — for tacos, Asian dishes, salsa.",
        "category": "produce", "subcategory": "herb",
        "keywords": [
            "cilantro", "coriander", "fresh coriander", "кориандър", "кинза",
        ],
        "unit": "bunch", "unit_size": "50g", "stock": 30,
        "image": _img("1576539378316-e0f97a7d3e47"),
        "price_eur": 0.95,
    },
    {
        "id": "ZP-BASIL-30",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Босилек Връзка",
        "name_en": "Basil Bunch",
        "description_bg": "Свеж босилек — за песто, капрезе, паста.",
        "description_en": "Fresh basil — for pesto, caprese, pasta.",
        "category": "produce", "subcategory": "herb",
        "keywords": [
            "basil", "fresh basil", "italian basil", "босилек", "pesto",
            "caprese",
        ],
        "unit": "bunch", "unit_size": "30g", "stock": 25,
        "image": _img("1567243345554-3dd6a76de8a9"),
        "price_eur": 1.20,
    },
    {
        "id": "ZP-MINT-30",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Мента Връзка",
        "name_en": "Mint Bunch",
        "description_bg": "Свежа мента — за чай, mojito, табуле.",
        "description_en": "Fresh mint — for tea, mojitos, tabbouleh.",
        "category": "produce", "subcategory": "herb",
        "keywords": [
            "mint", "spearmint", "fresh mint", "мента", "mojito",
        ],
        "unit": "bunch", "unit_size": "30g", "stock": 25,
        "image": _img("1628557044797-f21a177c37ec"),
        "price_eur": 1.00,
    },
    {
        "id": "ZP-AVOCADO-2",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Авокадо 2бр",
        "name_en": "Avocados 2 pcs",
        "description_bg": "Зрели Hass авокадо — за гуакамоле, тостове, салати.",
        "description_en": "Ripe Hass avocados — for guacamole, toast, salads.",
        "category": "produce", "subcategory": "fruit",
        "keywords": [
            "avocado", "hass avocado", "guacamole", "авокадо",
        ],
        "unit": "pack", "unit_size": "2 pcs", "stock": 30,
        "image": _img("1601039641847-7857b994d704"),
        "price_eur": 2.40,
    },
    {
        "id": "ZP-APPLES-1KG",
        "shop_id": "zelenchukov-pazar",
        "name_bg": "Ябълки 1кг",
        "name_en": "Apples 1kg",
        "description_bg": "Български Червен Делишес — десерт, печени ябълки, salads.",
        "description_en": "Bulgarian Red Delicious — dessert, baked apples, salads.",
        "category": "produce", "subcategory": "fruit",
        "keywords": [
            "apple", "apples", "red apple", "ябълка", "ябълки",
        ],
        "unit": "kg", "unit_size": "1kg", "stock": 30,
        "image": _img("1568702846914-96b305d2aaeb"),
        "price_eur": 1.80,
    },
    # ── furna-stefan (bakery) ──────────────────────────────────────────
    {
        "id": "FS-TORTILLA-WHEAT-8",
        "shop_id": "furna-stefan",
        "name_bg": "Тортила Питки 8бр",
        "name_en": "Wheat Tortilla Wraps 8 pcs",
        "description_bg": "Меки пшенични тортили — за burritos, quesadillas, wraps.",
        "description_en": "Soft wheat tortillas — for burritos, quesadillas, wraps.",
        "category": "bakery", "subcategory": "wrap",
        "keywords": [
            "tortilla", "wrap", "tortilla wrap", "wheat tortilla",
            "burrito", "quesadilla", "тортила", "питка",
        ],
        "unit": "pack", "unit_size": "8 pcs", "stock": 40,
        "image": _img("1565299585323-38d6b0865b47"),
        "price_eur": 2.95, "cuisine": ["mexican"],
    },
    {
        "id": "FS-PITA-6",
        "shop_id": "furna-stefan",
        "name_bg": "Пита Хлебчета 6бр",
        "name_en": "Pita Breads 6 pcs",
        "description_bg": "Меки питки — за gyros, falafel, mezze platters.",
        "description_en": "Soft pita pockets — for gyros, falafel, mezze platters.",
        "category": "bakery", "subcategory": "flatbread",
        "keywords": [
            "pita", "pita bread", "flatbread", "питка", "питки", "gyros",
            "falafel", "souvlaki",
        ],
        "unit": "pack", "unit_size": "6 pcs", "stock": 35,
        "image": _img("1601050690597-df0568f70950"),
        "price_eur": 2.40, "cuisine": ["middle-eastern"],
    },
    {
        "id": "FS-SANDWICH-LOAF",
        "shop_id": "furna-stefan",
        "name_bg": "Хляб за Сандвичи 500г",
        "name_en": "Sandwich Loaf 500g",
        "description_bg": "Мек бял хляб на филии — за сандвичи, тостове.",
        "description_en": "Soft sliced white loaf — for sandwiches and toast.",
        "category": "bakery", "subcategory": "bread",
        "keywords": [
            "white bread", "sandwich bread", "sliced bread", "loaf",
            "хляб", "филия", "sandwich",
        ],
        "unit": "pc", "unit_size": "500g", "stock": 28,
        "image": _img("1568471173242-461f0a730452"),
        "price_eur": 2.10,
    },
    # ── kvartalen-bakal (pantry) ───────────────────────────────────────
    {
        "id": "KB-RICE-LONG-1KG",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Дългозърнест Ориз 1кг",
        "name_en": "Long-Grain Rice 1kg",
        "description_bg": "Универсален дългозърнест ориз — за гарнитури, ризото, пилаф.",
        "description_en": "All-purpose long-grain rice — for sides, risotto, pilaf.",
        "category": "pantry", "subcategory": "rice",
        "keywords": [
            "rice", "long grain rice", "white rice", "basmati", "jasmine",
            "ориз", "pilaf", "ризото",
        ],
        "unit": "pack", "unit_size": "1kg", "stock": 60,
        "image": _img("1586201375761-83865001e31c"),
        "price_eur": 2.20,
    },
    {
        "id": "KB-OLIVE-OIL-500",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Маслиново Масло 500мл",
        "name_en": "Extra Virgin Olive Oil 500ml",
        "description_bg": "Студено пресовано маслиново масло — за салати, паста, печене.",
        "description_en": "Cold-pressed extra virgin olive oil — for salads, pasta, roasting.",
        "category": "pantry", "subcategory": "oil",
        "keywords": [
            "olive oil", "extra virgin olive oil", "evoo", "маслиново масло",
            "зехтин",
        ],
        "unit": "bottle", "unit_size": "500ml", "stock": 35,
        "image": _img("1474979266404-7eaacbcd87c5"),
        "price_eur": 6.95,
    },
    {
        "id": "KB-SALT-1KG",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Готварска Сол 1кг",
        "name_en": "Cooking Salt 1kg",
        "description_bg": "Фина готварска сол — за всякакво готвене.",
        "description_en": "Fine cooking salt — for all cooking.",
        "category": "pantry", "subcategory": "salt",
        "keywords": ["salt", "cooking salt", "fine salt", "сол"],
        "unit": "pack", "unit_size": "1kg", "stock": 80,
        "image": _img("1518110925495-b37f25154a91"),
        "price_eur": 0.85,
    },
    {
        "id": "KB-SUGAR-1KG",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Бяла Захар 1кг",
        "name_en": "White Sugar 1kg",
        "description_bg": "Кристална захар — за сладки, кафе, маринати.",
        "description_en": "Granulated white sugar — for baking, coffee, marinades.",
        "category": "pantry", "subcategory": "sugar",
        "keywords": ["sugar", "white sugar", "granulated sugar", "захар"],
        "unit": "pack", "unit_size": "1kg", "stock": 60,
        "image": _img("1581600140682-d4e68c8e3d1c"),
        "price_eur": 1.20,
    },
    {
        "id": "KB-CANNED-TOMATOES-400",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Белени Домати 400г",
        "name_en": "Whole Peeled Tomatoes 400g",
        "description_bg": "Италиански белени домати в кутия — за паста, ragù, супи.",
        "description_en": "Italian peeled tomatoes in tin — for pasta, ragù, soups.",
        "category": "canned", "subcategory": "tomato",
        "keywords": [
            "canned tomatoes", "peeled tomatoes", "san marzano", "tinned tomato",
            "домати в консерва", "белени домати",
        ],
        "unit": "tin", "unit_size": "400g", "stock": 70,
        "image": _img("1592924357229-71f95b8bba0e"),
        "price_eur": 1.45,
    },
    {
        "id": "KB-TOMATO-PASTE-200",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Доматено Пюре 200г",
        "name_en": "Tomato Paste 200g",
        "description_bg": "Концентрирано доматено пюре — за сосове, лютеница, ragù.",
        "description_en": "Concentrated tomato paste — for sauces, lyutenitsa, ragù.",
        "category": "canned", "subcategory": "tomato",
        "keywords": [
            "tomato paste", "tomato puree", "concentrate", "доматено пюре",
        ],
        "unit": "tube", "unit_size": "200g", "stock": 50,
        "image": _img("1592924357230-71f95b8bba0e"),
        "price_eur": 1.10,
    },
    {
        "id": "KB-CANNED-BEANS-400",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Бял Боб Консерва 400г",
        "name_en": "White Beans (Tin) 400g",
        "description_bg": "Готови за употреба бели бобови зърна — за супи, salads, chilli.",
        "description_en": "Ready-to-use white beans — for soups, salads, chilli.",
        "category": "canned", "subcategory": "legumes",
        "keywords": [
            "white beans", "cannellini", "navy beans", "canned beans",
            "боб", "бял боб",
        ],
        "unit": "tin", "unit_size": "400g", "stock": 50,
        "image": _img("1604927116313-77e4d24d18d4"),
        "price_eur": 1.30,
    },
    {
        "id": "KB-PASTA-PENNE-500",
        "shop_id": "kvartalen-bakal",
        "name_bg": "Пенне Паста 500г",
        "name_en": "Penne Pasta 500g",
        "description_bg": "Италиански пенне — за класически паста рецепти.",
        "description_en": "Italian penne — for classic pasta dishes.",
        "category": "pantry", "subcategory": "pasta",
        "keywords": [
            "penne", "pasta", "паста", "pasta penne",
        ],
        "unit": "pack", "unit_size": "500g", "stock": 60,
        "image": _img("1551892374-ecf8754cf8b0"),
        "price_eur": 1.45,
    },
    # ── asia-market-sofia (specialty asian) ────────────────────────────
    {
        "id": "AM-RICE-NOODLES-200",
        "shop_id": "asia-market-sofia",
        "name_bg": "Оризови Нудъли 200г",
        "name_en": "Rice Noodles 200g",
        "description_bg": "Тънки оризови нудъли — за pho, pad thai, summer rolls.",
        "description_en": "Thin rice noodles — for pho, pad thai, summer rolls.",
        "category": "pantry", "subcategory": "noodles",
        "keywords": [
            "rice noodles", "vermicelli", "pho noodles", "pad thai noodles",
            "оризови нудъли",
        ],
        "unit": "pack", "unit_size": "200g", "stock": 40,
        "image": _img("1565299543923-37dd37887442"),
        "price_eur": 2.45, "cuisine": ["asian"],
    },
    {
        "id": "AM-RAMEN-NOODLES-4",
        "shop_id": "asia-market-sofia",
        "name_bg": "Рамен Нудъли 4бр",
        "name_en": "Ramen Noodles 4 pcs",
        "description_bg": "Японски рамен нудъли — fresh-style, за домашен ramen.",
        "description_en": "Japanese-style ramen noodles — for homemade ramen bowls.",
        "category": "pantry", "subcategory": "noodles",
        "keywords": [
            "ramen", "ramen noodles", "japanese noodles", "рамен",
        ],
        "unit": "pack", "unit_size": "4 pcs", "stock": 30,
        "image": _img("1591814468924-caf88d1232e1"),
        "price_eur": 3.95, "cuisine": ["asian"],
    },
    {
        "id": "AM-FISH-SAUCE-300",
        "shop_id": "asia-market-sofia",
        "name_bg": "Рибен Сос 300мл",
        "name_en": "Fish Sauce 300ml",
        "description_bg": "Тайландски рибен сос — за pad thai, dipping sauce, маринати.",
        "description_en": "Thai fish sauce — for pad thai, dipping sauces, marinades.",
        "category": "condiment", "subcategory": "sauce",
        "keywords": [
            "fish sauce", "nuoc mam", "nam pla", "рибен сос",
        ],
        "unit": "bottle", "unit_size": "300ml", "stock": 25,
        "image": _img("1604908176997-125f25cc6f3d"),
        "price_eur": 4.50, "cuisine": ["asian"],
    },
    {
        "id": "AM-OYSTER-SAUCE-250",
        "shop_id": "asia-market-sofia",
        "name_bg": "Оустър Сос 250мл",
        "name_en": "Oyster Sauce 250ml",
        "description_bg": "Гъст оустър сос — за stir fry, мариновки, sauces.",
        "description_en": "Thick oyster sauce — for stir-fries, marinades, sauces.",
        "category": "condiment", "subcategory": "sauce",
        "keywords": [
            "oyster sauce", "stir fry sauce", "оустър сос",
        ],
        "unit": "bottle", "unit_size": "250ml", "stock": 25,
        "image": _img("1610440042657-c2cd55c5b88c"),
        "price_eur": 3.95, "cuisine": ["asian"],
    },
    {
        "id": "AM-COCONUT-MILK-400",
        "shop_id": "asia-market-sofia",
        "name_bg": "Кокосово Мляко 400мл",
        "name_en": "Coconut Milk 400ml",
        "description_bg": "Гъсто кокосово мляко — за curry, supas, десерти.",
        "description_en": "Thick coconut milk — for curries, soups, desserts.",
        "category": "canned", "subcategory": "coconut",
        "keywords": [
            "coconut milk", "coconut cream", "кокосово мляко",
        ],
        "unit": "tin", "unit_size": "400ml", "stock": 40,
        "image": _img("1571115332474-d0e34c3d8c08"),
        "price_eur": 2.45, "cuisine": ["asian"],
    },
    {
        "id": "AM-TOFU-FIRM-400",
        "shop_id": "asia-market-sofia",
        "name_bg": "Тофу Firm 400г",
        "name_en": "Firm Tofu 400g",
        "description_bg": "Плътно тофу — за stir fry, печено, scramble.",
        "description_en": "Firm tofu — for stir-fries, baking, scrambles.",
        "category": "specialty", "subcategory": "tofu",
        "keywords": [
            "tofu", "firm tofu", "soy tofu", "тофу",
        ],
        "unit": "pack", "unit_size": "400g", "stock": 25,
        "image": _img("1606728035253-49e8a23146de"),
        "price_eur": 3.20, "cuisine": ["asian"],
    },
    {
        "id": "AM-SRIRACHA-250",
        "shop_id": "asia-market-sofia",
        "name_bg": "Шрирача Сос 250мл",
        "name_en": "Sriracha Hot Sauce 250ml",
        "description_bg": "Класическа шрирача — за пържоли, ориз, сандвичи.",
        "description_en": "Classic sriracha — for grilled meats, rice, sandwiches.",
        "category": "condiment", "subcategory": "hot-sauce",
        "keywords": [
            "sriracha", "hot sauce", "chili sauce", "шрирача",
        ],
        "unit": "bottle", "unit_size": "250ml", "stock": 30,
        "image": _img("1599909366516-6c1c7a6e8df0"),
        "price_eur": 3.20, "cuisine": ["asian"],
    },
]


# ── migration core ───────────────────────────────────────────────────────


def migrate_one(product: dict) -> dict:
    """Idempotently normalise a product dict in-place; returns the dict."""
    legacy_cat = product.get("category")
    if "subcategory" not in product and legacy_cat in CATEGORY_MAP:
        top, sub, cuisine = CATEGORY_MAP[legacy_cat]
        product["category"] = top
        product["subcategory"] = sub
        if cuisine and "cuisine" not in product:
            product["cuisine"] = cuisine
    elif "subcategory" not in product:
        # unknown legacy → leave category, set subcategory = legacy to preserve info
        product["subcategory"] = product.get("category") or "other"

    if name_en := product.get("name_en"):
        product["name_en"] = title_case(name_en)
    if name_bg := product.get("name_bg"):
        product["name_bg"] = title_case(name_bg)
    return product


def main() -> None:
    products = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    recipe_products = json.loads(RECIPE_PRODUCTS.read_text(encoding="utf-8"))

    for p in products:
        migrate_one(p)
    for p in recipe_products:
        migrate_one(p)

    existing_ids = {p["id"] for p in recipe_products}
    added = 0
    for new in NEW_STAPLES:
        if new["id"] in existing_ids:
            continue
        recipe_products.append(new)
        added += 1

    PRODUCTS.write_text(
        json.dumps(products, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    RECIPE_PRODUCTS.write_text(
        json.dumps(recipe_products, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"products.json: {len(products)} entries; "
        f"recipe_products.json: {len(recipe_products)} entries (+{added} staples)"
    )


if __name__ == "__main__":
    main()
