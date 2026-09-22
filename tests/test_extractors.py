"""Unit tests for scraper.extractors - offline HTML fixtures, no network."""

import unittest

from scraper.extractors import (
    clean_product_name,
    extract,
    extract_from_json_ld,
    extract_from_meta,
    extract_from_selectors,
    extract_from_text,
    extract_packages,
    final_idr_price,
    parse_idr_price,
    parse_idr_prices,
    parse_units,
)


class TestParseIdrPrice(unittest.TestCase):
    def test_rp_prefix(self):
        self.assertEqual(parse_idr_price("Rp 199.000"), 199000)

    def test_rp_no_space(self):
        self.assertEqual(parse_idr_price("Rp199.000"), 199000)

    def test_idr_suffix(self):
        self.assertEqual(parse_idr_price("199.000 IDR"), 199000)

    def test_millions(self):
        self.assertEqual(parse_idr_price("Rp 1.250.000"), 1250000)

    def test_us_format_comma_thousands(self):
        self.assertEqual(parse_idr_price("Rp 199,000"), 199000)

    def test_rejects_unit_only_text(self):
        self.assertIsNone(parse_idr_price("86 Diamonds + 8 Bonus"))

    def test_rejects_garbage(self):
        self.assertIsNone(parse_idr_price("no price here"))
        self.assertIsNone(parse_idr_price(""))


class TestParseUnits(unittest.TestCase):
    def test_base_and_bonus(self):
        self.assertEqual(parse_units("86 Diamonds + 8 Bonus"), (86, 8))

    def test_dana_composition_format(self):
        # '14 Diamonds (13 + 1 Bonus)' = 14 total as 13 base + 1 bonus.
        self.assertEqual(parse_units("14 Diamonds (13 + 1 Bonus) Rp5.236 Rp4.760"), (13, 1))

    def test_dana_composition_without_bonus(self):
        self.assertEqual(parse_units("70 Diamonds (70 + 0 Bonus)"), (70, 0))

    def test_dana_composition_without_bonus_word(self):
        # Codashop style: '300 Diamonds (150+150)' = 300 total as 150+150.
        self.assertEqual(
            parse_units("300 Diamonds (150+150) pengisian pertama! Dari -31%"), (150, 150)
        )

    def test_unipin_reverse_composition(self):
        self.assertEqual(parse_units("11 + 1 Diamonds"), (11, 1))
        self.assertEqual(parse_units("40 + 4 Diamonds Off"), (40, 4))

    def test_base_only(self):
        self.assertEqual(parse_units("70 Diamonds"), (70, 0))

    def test_diamond_abbreviation(self):
        self.assertEqual(parse_units("258 Dm"), (258, 0))

    def test_bonus_capped_at_base(self):
        # A nonsensical bonus (>= base) is rejected -> treated as no bonus.
        self.assertEqual(parse_units("86 Diamonds + 999 Bonus"), (86, 0))

    def test_no_units(self):
        self.assertIsNone(parse_units("Weekly Diamond Pass"))
        self.assertIsNone(parse_units(""))


class TestJsonLd(unittest.TestCase):
    def test_product_with_offer(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@type": "Product", "name": "86 Diamonds + 8 Bonus",
         "offers": {"@type": "Offer", "price": 199000, "priceCurrency": "IDR"}}
        </script></head><body></body></html>
        """
        result = extract_from_json_ld(__import__("bs4").BeautifulSoup(html, "html.parser"))
        self.assertIsNotNone(result)
        self.assertEqual(result["total_price"], 199000)
        self.assertEqual(result["base_units"], 86)
        self.assertEqual(result["bonus_units"], 8)
        self.assertEqual(result["method"], "json_ld")

    def test_non_idr_rejected(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@type": "Product", "name": "86 Diamonds", "offers": {"price": 12.99, "priceCurrency": "USD"}}
        </script></head><body></body></html>
        """
        soup = __import__("bs4").BeautifulSoup(html, "html.parser")
        self.assertIsNone(extract_from_json_ld(soup))


class TestMeta(unittest.TestCase):
    def test_og_price_with_title_units(self):
        html = """
        <html><head>
        <meta property="og:price:amount" content="199000"/>
        <meta property="og:title" content="86 Diamonds + 8 Bonus - Top Up"/>
        </head><body></body></html>
        """
        result = extract_from_meta(__import__("bs4").BeautifulSoup(html, "html.parser"))
        self.assertIsNotNone(result)
        self.assertEqual(result["total_price"], 199000)
        self.assertEqual(result["base_units"], 86)


class TestSelectors(unittest.TestCase):
    def test_configured_selectors(self):
        html = """
        <html><body>
        <h1>86 Diamonds + 8 Bonus</h1>
        <span class="price">Rp 199.000</span>
        </body></html>
        """
        soup = __import__("bs4").BeautifulSoup(html, "html.parser")
        result = extract_from_selectors(soup, ".price", None)
        self.assertIsNotNone(result)
        self.assertEqual(result["total_price"], 199000)
        self.assertEqual(result["base_units"], 86)

    def test_missing_selector(self):
        soup = __import__("bs4").BeautifulSoup("<html><body></body></html>", "html.parser")
        self.assertIsNone(extract_from_selectors(soup, ".nonexistent", None))
        self.assertIsNone(extract_from_selectors(soup, None, None))


class TestTextHeuristics(unittest.TestCase):
    def test_card_layout(self):
        html = """
        <html><body>
        <div class="card">
            <span>86 Diamonds + 8 Bonus</span>
            <span class="amount">Rp 199.000</span>
        </div>
        <div class="card">
            <span>172 Diamonds + 16 Bonus</span>
            <span class="amount">Rp 399.000</span>
        </div>
        </body></html>
        """
        soup = __import__("bs4").BeautifulSoup(html, "html.parser")
        result = extract_from_text(soup)
        self.assertIsNotNone(result)
        self.assertIn(result["total_price"], (199000, 399000))
        self.assertIn(result["base_units"], (86, 172))


class TestOrchestrator(unittest.TestCase):
    def test_prefers_json_ld_over_text(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@type": "Product", "name": "344 Diamonds + 52 Bonus",
         "offers": {"price": 799000, "priceCurrency": "IDR"}}
        </script></head><body>
        <div>86 Diamonds + 8 Bonus Rp 199.000</div>
        </body></html>
        """
        result = extract(html)
        self.assertIsNotNone(result)
        self.assertEqual(result["method"], "json_ld")
        self.assertEqual(result["total_price"], 799000)

    def test_nothing_found(self):
        result = extract("<html><body><p>hello</p></body></html>")
        self.assertIsNone(result)


class TestFinalPrice(unittest.TestCase):
    def test_all_prices_in_order(self):
        self.assertEqual(
            parse_idr_prices("5 Diamonds Rp1.150 Rp1.000"), [1150, 1000]
        )

    def test_final_price_is_last(self):
        self.assertEqual(final_idr_price("5 Diamonds Rp1.150 Rp1.000"), 1000)

    def test_single_price(self):
        self.assertEqual(final_idr_price("Rp 199.000"), 199000)

    def test_clean_product_name(self):
        self.assertEqual(clean_product_name("5 Diamonds Rp1.150 Rp1.000"), "5 Diamonds")
        self.assertEqual(clean_product_name("86 Diamonds + 8 Bonus"), "86 Diamonds + 8 Bonus")
        self.assertEqual(
            clean_product_name("Weekly Diamond pass Sat Set Murah"), "Weekly Diamond pass"
        )
        self.assertEqual(
            clean_product_name("100 Diamonds (50+50) pengisian pertama! Dari -31%"),
            "100 Diamonds (50+50)",
        )


class TestExtractPackages(unittest.TestCase):
    def test_multiple_cards_with_discounted_price(self):
        html = """
        <html><body>
        <div class="card"><span>5 Diamonds Rp1.150 Rp1.000</span></div>
        <div class="card"><span>70 Diamonds Rp11.500 Rp10.500</span></div>
        <div class="card"><span>140 Diamonds Rp23.000 Rp21.000</span></div>
        </body></html>
        """
        packages = extract_packages(html)
        self.assertEqual(len(packages), 3)
        by_units = {p["base_units"]: p for p in packages}
        self.assertEqual(by_units[5]["total_price"], 1000)  # final, not 1150
        self.assertEqual(by_units[5]["product_name"], "5 Diamonds")
        self.assertEqual(by_units[70]["total_price"], 10500)
        self.assertEqual(by_units[140]["total_price"], 21000)

    def test_pass_product_without_units(self):
        html = """
        <html><body>
        <div class="card"><span>Weekly Diamond Pass Rp16.000 Rp15.000</span></div>
        </body></html>
        """
        packages = extract_packages(html)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["base_units"], 0)
        self.assertEqual(packages[0]["product_name"], "Weekly Diamond Pass")
        self.assertEqual(packages[0]["total_price"], 15000)

    def test_dedupes_same_units(self):
        html = """
        <html><body>
        <div><span>70 Diamonds Rp11.500</span></div>
        <div><span>70 Diamonds Rp11.500</span></div>
        </body></html>
        """
        self.assertEqual(len(extract_packages(html)), 1)

    def test_ignores_grid_scope(self):
        # A giant container with many cards must not become one bogus package
        # pairing the first units with the last price.
        html = """
        <html><body>
        <div class="grid">
        <div><span>70 Diamonds</span><span>Rp 11.500</span></div>
        <div><span>140 Diamonds</span><span>Rp 23.000</span></div>
        <div><span>355 Diamonds</span><span>Rp 57.500</span></div>
        </div>
        </body></html>
        """
        packages = extract_packages(html)
        self.assertEqual(len(packages), 3)
        by_units = {p["base_units"]: p for p in packages}
        self.assertEqual(by_units[70]["total_price"], 11500)
        self.assertEqual(by_units[355]["total_price"], 57500)


if __name__ == "__main__":
    unittest.main()
