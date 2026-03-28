#!/usr/bin/env python3
"""Tests for the filing-websites pipeline."""

import json
import os
import sys
import unittest

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from pipeline import (
    validate_data,
    _get_unit_multiplier,
    _is_top_level_heading,
    _extract_title,
    esc,
    fmt_num,
    render_html,
    strip_code_fences,
)


class TestValidateData(unittest.TestCase):
    """Test the validate_data function (auto-fixes + warnings)."""

    def _make_data(self, **overrides):
        base = {
            'meta': {'issuer': 'Test Corp', 'filing_date': '2025-01-01'},
            'company': {
                'name_en': 'Test Corp', 'name_cn': '测试公司',
                'founded': '2020', 'headquarters': 'Beijing',
                'industry': 'AI', 'controller': {'name': 'John', 'roles': ['CEO']}
            },
            'financials': {
                'currency': 'RMB', 'unit': '10K (万元)',
                'income_statement': [{'period': '2024', 'revenue': 10000, 'net_profit': 5000, 'gross_margin_pct': 50}],
                'balance_sheet': [{'date': '2024-12-31', 'total_assets': 50000}],
            },
            'shareholders_pre_ipo': [
                {'name': 'Founder', 'pct': 30, 'type': 'Individual'},
                {'name': 'VC Fund', 'pct': 20, 'type': 'VC'},
            ],
            'key_risks': ['Risk one is about competition', 'Risk two is about regulation', 'Risk three is about market'],
            'products': [{'name': 'Product A'}],
            'use_of_proceeds': {'projects': [{'name': 'R&D'}]},
        }
        for k, v in overrides.items():
            if '.' in k:
                parts = k.split('.')
                d = base
                for p in parts[:-1]:
                    d = d[p]
                d[parts[-1]] = v
            else:
                base[k] = v
        return base

    def test_valid_data_passes(self):
        data = self._make_data()
        result = validate_data(data)
        self.assertTrue(result)

    def test_fixes_negative_revenue(self):
        data = self._make_data()
        data['financials']['income_statement'][0]['revenue'] = -100
        validate_data(data)
        self.assertIsNone(data['financials']['income_statement'][0]['revenue'])

    def test_fixes_100pct_margin_on_zero_revenue(self):
        data = self._make_data()
        data['financials']['income_statement'][0]['revenue'] = 0.01
        data['financials']['income_statement'][0]['gross_margin_pct'] = 100
        validate_data(data)
        self.assertIsNone(data['financials']['income_statement'][0]['gross_margin_pct'])

    def test_fixes_extreme_margin(self):
        data = self._make_data()
        data['financials']['income_statement'][0]['gross_margin_pct'] = 99.5
        validate_data(data)
        self.assertIsNone(data['financials']['income_statement'][0]['gross_margin_pct'])

    def test_removes_catch_all_shareholders(self):
        data = self._make_data()
        data['shareholders_pre_ipo'] = [
            {'name': 'Founder', 'pct': 30},
            {'name': 'VC', 'pct': 20},
            {'name': 'Other Pre-IPO Investors', 'pct': 70},
        ]
        validate_data(data)
        names = [s['name'] for s in data['shareholders_pre_ipo']]
        self.assertNotIn('Other Pre-IPO Investors', names)

    def test_removes_duplicate_shareholders(self):
        data = self._make_data()
        data['shareholders_pre_ipo'] = [
            {'name': 'Founder', 'pct': 30},
            {'name': 'Founder', 'pct': 30},
            {'name': 'VC', 'pct': 20},
        ]
        validate_data(data)
        self.assertEqual(len(data['shareholders_pre_ipo']), 2)

    def test_removes_short_risks(self):
        data = self._make_data()
        data['key_risks'] = ['OK risk about competition and market dynamics', 'too short', 'Another valid risk about regulatory changes in the market']
        validate_data(data)
        self.assertEqual(len(data['key_risks']), 2)

    def test_warns_missing_controller(self):
        data = self._make_data()
        data['company']['controller'] = {}
        result = validate_data(data)
        self.assertFalse(result)


class TestGetUnitMultiplier(unittest.TestCase):
    """Test currency unit detection."""

    def test_rmb_wan(self):
        data = {'financials': {'unit': '10K (万元)', 'currency': 'RMB'}}
        mult, is_usd = _get_unit_multiplier(data)
        self.assertEqual(mult, 10000)
        self.assertFalse(is_usd)

    def test_usd_thousands(self):
        data = {'financials': {'unit': 'thousands (US$)', 'currency': 'USD'}}
        mult, is_usd = _get_unit_multiplier(data)
        self.assertEqual(mult, 1000)
        self.assertTrue(is_usd)

    def test_hkd(self):
        data = {'financials': {'unit': '10K', 'currency': 'HKD'}}
        mult, is_usd = _get_unit_multiplier(data)
        self.assertAlmostEqual(mult, 10000 * 0.128)
        self.assertTrue(is_usd)

    def test_rmb_default(self):
        data = {'financials': {'unit': '', 'currency': ''}}
        mult, is_usd = _get_unit_multiplier(data)
        self.assertEqual(mult, 1)
        self.assertFalse(is_usd)

    def test_rmb_yi(self):
        data = {'financials': {'unit': '亿元', 'currency': 'RMB'}}
        mult, is_usd = _get_unit_multiplier(data)
        self.assertEqual(mult, 100000000)
        self.assertFalse(is_usd)


class TestIsTopLevelHeading(unittest.TestCase):
    """Test heading classification for TOC."""

    def test_section_heading(self):
        self.assertTrue(_is_top_level_heading('Section 1 Definitions'))
        self.assertTrue(_is_top_level_heading('Section II Overview'))
        self.assertTrue(_is_top_level_heading('Risk Factors'))

    def test_roman_numeral_sub(self):
        self.assertFalse(_is_top_level_heading('VIII. Special Voting Rights'))
        self.assertFalse(_is_top_level_heading('III. Sales Conditions'))

    def test_parenthetical_sub(self):
        self.assertFalse(_is_top_level_heading('(IV) Inventories'))
        self.assertFalse(_is_top_level_heading('(1) Revenue breakdown'))

    def test_decimal_sub(self):
        self.assertFalse(_is_top_level_heading('3.2 Market Analysis'))


class TestExtractTitle(unittest.TestCase):
    """Test title extraction from HTML chunks."""

    def test_extracts_h2(self):
        html = '<h2>Risk Factors</h2><p>Some text</p>'
        self.assertEqual(_extract_title(html, 0), 'Risk Factors')

    def test_skips_company_name(self):
        html = '<h2>Unitree Technology Co., Ltd. — Prospectus</h2><h2>Risk Factors</h2>'
        self.assertEqual(_extract_title(html, 0), 'Risk Factors')

    def test_fallback_to_paragraph(self):
        html = '<p>This is a substantial paragraph title</p><p>More content here.</p>'
        self.assertEqual(_extract_title(html, 0), 'This is a substantial paragraph title')

    def test_fallback_to_part_n(self):
        html = '<p>Hi</p>'
        self.assertEqual(_extract_title(html, 2), 'Part 3')


class TestHelpers(unittest.TestCase):
    """Test utility functions."""

    def test_esc(self):
        self.assertEqual(esc('<script>'), '&lt;script&gt;')
        self.assertEqual(esc(None), '')
        self.assertEqual(esc(42), '42')

    def test_fmt_num(self):
        self.assertEqual(fmt_num(None), 'N/A')
        self.assertEqual(fmt_num(1234567), '1,234,567')
        self.assertEqual(fmt_num('abc'), 'abc')

    def test_strip_code_fences(self):
        self.assertEqual(strip_code_fences('```json\n{"a":1}\n```'), '{"a":1}')
        self.assertEqual(strip_code_fences('{"a":1}'), '{"a":1}')
        self.assertEqual(strip_code_fences('```\nfoo\n```'), 'foo')


class TestRenderHtml(unittest.TestCase):
    """Test HTML template rendering."""

    def _make_minimal_data(self):
        return {
            'meta': {'issuer': 'Test Corp', 'exchange': 'HKEX', 'board': 'Main', 'filing_date': '2025-01-01'},
            'company': {'name_cn': '测试公司', 'industry': 'AI'},
            'financials': {'currency': 'RMB', 'unit': '10K (万元)',
                          'income_statement': [{'period': '2024', 'revenue': 10000}],
                          'balance_sheet': []},
            'shareholders_pre_ipo': [{'name': 'Founder', 'pct': 30, 'type': 'Individual'}],
            'key_risks': ['Competition risk in the AI market is significant'],
            'use_of_proceeds': {'projects': [{'name': 'R&D', 'amount_rmb_10k': 5000, 'focus': 'Research'}]},
            'products': [],
            'offering': {},
            'executive_summary': 'Test Corp is an AI company raising funds.',
        }

    def test_renders_complete_html(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        self.assertTrue(html.strip().endswith('</html>'))
        self.assertIn('Test Corp', html)
        self.assertIn('测试公司', html)

    def test_includes_executive_summary(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        self.assertIn('AI-Generated Summary', html)
        self.assertIn('Test Corp is an AI company', html)

    def test_financial_values_in_usd(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        # 10000 万 * 10000 / 7.25 = ~$13.8M
        self.assertIn('$14M', html)

    def test_theme_toggle(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        self.assertIn('toggleTheme', html)
        self.assertIn('data-theme', html)

    def test_search_functions(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        self.assertIn('keywordSearch', html)
        self.assertIn('callLLM', html)
        self.assertIn('renderMd', html)

    def test_back_link_to_index(self):
        data = self._make_minimal_data()
        html = render_html(data, None)
        self.assertIn('href="../"', html)

    def test_no_stray_section_tags_in_accordion(self):
        """Translation content with <section> tags shouldn't break accordion."""
        data = self._make_minimal_data()
        full_text = {
            'sections': [{
                'id': 'test',
                'title_en': 'Test Section',
                'content': '<h2>Overview</h2><section>bad tag</section><p>Content</p>'
            }]
        }
        html = render_html(data, full_text)
        # Count section opens vs closes
        opens = html.count('<section')
        closes = html.count('</section>')
        self.assertEqual(opens, closes, f'Unbalanced section tags: {opens} opens vs {closes} closes')


class TestExistingData(unittest.TestCase):
    """Test that all existing data.json files pass validation."""

    def test_all_companies_have_valid_data(self):
        """Every company's data.json should pass validate_data without errors."""
        root = os.path.dirname(__file__)
        for entry in os.listdir(root):
            data_path = os.path.join(root, entry, 'data.json')
            if os.path.isfile(data_path):
                with open(data_path) as f:
                    data = json.load(f)
                # validate_data modifies in place and returns bool
                # We just check it doesn't crash
                validate_data(data)

    def _get_active_slugs(self):
        root = os.path.dirname(__file__)
        manifest = os.path.join(root, 'filings.json')
        if os.path.exists(manifest):
            with open(manifest) as f:
                return {f['slug'] for f in json.load(f)}
        return set()

    def test_all_companies_have_executive_summary(self):
        root = os.path.dirname(__file__)
        active = self._get_active_slugs()
        for entry in os.listdir(root):
            if entry not in active:
                continue
            data_path = os.path.join(root, entry, 'data.json')
            if os.path.isfile(data_path):
                with open(data_path) as f:
                    data = json.load(f)
                self.assertTrue(
                    data.get('executive_summary'),
                    f'{entry} missing executive_summary'
                )

    def test_all_html_files_complete(self):
        """Every index.html should end with </html>."""
        root = os.path.dirname(__file__)
        for entry in os.listdir(root):
            html_path = os.path.join(root, entry, 'index.html')
            if os.path.isfile(html_path):
                with open(html_path) as f:
                    html = f.read()
                self.assertTrue(
                    html.strip().endswith('</html>'),
                    f'{entry}/index.html is truncated'
                )


if __name__ == '__main__':
    unittest.main()
