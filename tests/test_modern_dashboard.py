import json
import unittest

from modern_dashboard import parse_modern_dashboard


def flight_scripts(value, cuts=()):
    stream = 'a:' + json.dumps(value, ensure_ascii=False) + '\n'
    pieces = []
    start = 0
    for end in (*cuts, len(stream)):
        pieces.append('self.__next_f.push(' + json.dumps([1, stream[start:end]]) + ');')
        start = end
    return pieces


def daily_item(**changes):
    item = dict(date='10/25/2026', offerId='Gamification_DailySet_Test_Child1',
                title='Daily activity', destination='https://www.bing.com/search?q=test',
                isCompleted=False, isLocked='$undefined', isUnlocked='$undefined',
                hash='must-not-copy', debugInfo={'account': 'must-not-copy'}, points=10)
    return item | changes


class ModernDashboardTests(unittest.TestCase):
    def test_extracts_nested_daily_items_and_copies_only_needed_fields(self):
        parsed = parse_modern_dashboard(flight_scripts(['$', 'component', None, {
            'account': {'token': 'must-not-copy'}, 'dailySetItems': [daily_item()]}]))
        self.assertEqual(parsed, {'dailySetPromotions': {'10/25/2026': [{
            'offerId': 'Gamification_DailySet_Test_Child1', 'title': 'Daily activity',
            'destinationUrl': 'https://www.bing.com/search?q=test', 'complete': False,
            'promotionType': 'urlreward', '__source': 'modern', '__locked': False}]}})

    def test_joins_chunks_split_inside_json_and_includes_multiple_pushes(self):
        scripts = flight_scripts({'dailySetItems': [daily_item()]}, cuts=(13, 80))
        scripts[0] = 'self.__next_f.push([0]);' + scripts[0]
        scripts[-2:] = [''.join(scripts[-2:])]
        parsed = parse_modern_dashboard(scripts)
        self.assertEqual(len(parsed['dailySetPromotions']['10/25/2026']), 1)

    def test_ignores_other_offer_lists_and_non_data_flight_chunks(self):
        scripts = ['self.__next_f.push(' + json.dumps([2, 'a:' + json.dumps({
            'dailySetItems': [daily_item(offerId='wrong')]}) + '\n']) + ');']
        scripts += flight_scripts({'offers': [daily_item(offerId='shopping')],
                                   'banners': [daily_item(offerId='redeem')],
                                   'dailySetItems': [daily_item(offerId='daily')]})
        parsed = parse_modern_dashboard(scripts)
        self.assertEqual([row['offerId'] for row in parsed['dailySetPromotions']['10/25/2026']], ['daily'])

    def test_preserves_unknown_completion_and_strict_lock_flags(self):
        items = [daily_item(offerId='unknown', isCompleted='true', isLocked='true'),
                 daily_item(offerId='locked', isCompleted=True, isLocked=True),
                 daily_item(offerId='not-unlocked', isUnlocked=False),
                 daily_item(offerId='unknown-flags', isCompleted=1, isLocked=1, isUnlocked=0)]
        rows = parse_modern_dashboard(flight_scripts({'dailySetItems': items}))['dailySetPromotions']['10/25/2026']
        self.assertEqual([row['complete'] for row in rows], [None, True, False, None])
        self.assertEqual([row['__locked'] for row in rows], [False, True, True, False])

    def test_normalizes_unpadded_and_iso_dates_without_guessing_today(self):
        items = [daily_item(date='1/5/2026', offerId='one'),
                 daily_item(date='2026-01-05', offerId='two'),
                 daily_item(date='not-a-date'), daily_item(date=None)]
        parsed = parse_modern_dashboard(flight_scripts({'dailySetItems': items}))
        self.assertEqual(list(parsed['dailySetPromotions']), ['01/05/2026'])
        self.assertEqual(len(parsed['dailySetPromotions']['01/05/2026']), 2)

    def test_skips_invalid_identifiers_and_leaves_bad_destinations_unusable(self):
        items = [None, 4, {}, daily_item(offerId=None), daily_item(offerId=''),
                 daily_item(offerId=' ', date='10/25/2026'),
                 daily_item(offerId='valid', title=None, destination={'url': 'https://bing.com/'})]
        rows = parse_modern_dashboard(flight_scripts({'dailySetItems': items}))['dailySetPromotions']['10/25/2026']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['offerId'], 'valid')
        self.assertEqual(rows[0]['title'], 'valid')
        self.assertEqual(rows[0]['destinationUrl'], '')

    def test_distinguishes_empty_daily_items_from_missing_or_wrong_schema(self):
        self.assertEqual(parse_modern_dashboard(flight_scripts({'dailySetItems': []})),
                         {'dailySetPromotions': {}})
        for value in ({'offers': []}, {'dailySetItems': None}, {'dailySetItems': '$a'}):
            with self.subTest(value=value):
                self.assertIsNone(parse_modern_dashboard(flight_scripts(value)))

    def test_does_not_evaluate_javascript_or_extract_unrelated_json(self):
        valid_argument = json.dumps([1, 'a:' + json.dumps({'dailySetItems': [daily_item()]}) + '\n'])
        for script in ('var unrelated = ' + valid_argument + ';',
                       'self.__next_f.push(makeData());',
                       'self.__next_f.push(' + valid_argument + ' + malicious());'):
            with self.subTest(script=script):
                self.assertIsNone(parse_modern_dashboard([script]))

    def test_ignores_non_json_flight_records_and_preserves_escaped_text(self):
        scripts = ['self.__next_f.push(' + json.dumps([1, '0:I["module"]\n1:HL["style"]\n']) + ');']
        scripts += flight_scripts({'dailySetItems': [daily_item(title='Line 1\n"quoted" 中文')]})
        row = parse_modern_dashboard(scripts)['dailySetPromotions']['10/25/2026'][0]
        self.assertEqual(row['title'], 'Line 1\n"quoted" 中文')

    def test_rejects_non_json_numeric_constants(self):
        stream = 'a:{"dailySetItems":[],"other":NaN}\n'
        self.assertIsNone(parse_modern_dashboard(['self.__next_f.push(' + json.dumps([1, stream]) + ');']))

    def test_extracts_dateless_earn_activities_without_copying_account_fields(self):
        item = daily_item(offerId='earn-offer', points=15, isPromotional='$undefined',
                          isLocked=False, isUnlocked=True)
        del item['date']
        parsed = parse_modern_dashboard(flight_scripts({'props': {'activityCards': [item]}}))
        self.assertEqual(parsed, {'dailySetPromotions': {}, 'activityPromotions': [{
            'offerId': 'earn-offer', 'title': 'Daily activity',
            'destinationUrl': 'https://www.bing.com/search?q=test', 'complete': False,
            'promotionType': 'urlreward', '__source': 'modern', '__locked': False,
            '__page': 'earn', 'points': 15}]})

    def test_filters_promotional_nonpositive_and_non_integer_earn_points(self):
        items = [daily_item(offerId='eligible', points=15, isPromotional=False),
                 daily_item(offerId='promo', points=15, isPromotional=True),
                 *[daily_item(offerId='bad-points', points=value)
                   for value in (0, -1, True, False, 15.0, '15', None)]]
        parsed = parse_modern_dashboard(flight_scripts({'activityCards': items}))
        self.assertEqual([row['offerId'] for row in parsed['activityPromotions']], ['eligible'])

    def test_earn_completion_and_lock_flags_require_exact_booleans(self):
        items = [daily_item(offerId='done', isCompleted=True, isLocked=False, isUnlocked=True),
                 daily_item(offerId='locked', isCompleted=False, isLocked=True),
                 daily_item(offerId='not-unlocked', isCompleted=None, isUnlocked=False),
                 daily_item(offerId='unknown', isCompleted='true', isLocked=1, isUnlocked=0)]
        rows = parse_modern_dashboard(flight_scripts({'activityCards': items}))['activityPromotions']
        self.assertEqual([row['complete'] for row in rows], [True, False, None, None])
        self.assertEqual([row['__locked'] for row in rows], [False, True, True, False])

    def test_earn_schema_is_optional_and_does_not_change_daily_mapping(self):
        daily = {'dailySetItems': [daily_item()], 'cards': [daily_item(offerId='unrelated')]}
        parsed = parse_modern_dashboard(flight_scripts(daily))
        self.assertNotIn('activityPromotions', parsed)
        combined = parse_modern_dashboard(flight_scripts(daily | {'activityCards': []}))
        self.assertEqual(combined['dailySetPromotions'], parsed['dailySetPromotions'])
        self.assertEqual(combined['activityPromotions'], [])
        self.assertEqual(parse_modern_dashboard(flight_scripts({'activityCards': []})),
                         {'dailySetPromotions': {}, 'activityPromotions': []})
        self.assertIsNone(parse_modern_dashboard(flight_scripts({'activityCards': '$a'})))


if __name__ == '__main__':
    unittest.main()
