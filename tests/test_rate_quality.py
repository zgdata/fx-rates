import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rate_quality import apply_quality, source_quality, ISO_CODES
import fetch_rates

NOW = dt.datetime(2026, 10, 13, 5, 30, tzinfo=dt.timezone.utc)


def quote(**extra):
    return dict(id='pbz', status='ok', fetched_at=NOW.isoformat(), valid_for='2026-10-13',
                rates={'USD': {'middle': 0.9}}, **extra)


class QualityTests(unittest.TestCase):
    def test_retrieval_does_not_refresh_an_old_bank_list(self):
        for name in ('pbz', 'zaba', 'rba', 'addiko'):
            bank = quote(); bank.update(id=name, valid_for='2026-10-10')
            actual = source_quality(bank, 'Europe/Zagreb', NOW)
            self.assertEqual(actual['status'], 'stale')
            self.assertEqual(actual['freshness'], 'stale')
            self.assertEqual(actual['rates'], bank['rates'])
            self.assertEqual(actual['valid_for'], '2026-10-10')
            self.assertEqual(actual['fetched_at'], bank['fetched_at'])

    def test_day_only_previous_day_is_not_given_an_invented_time(self):
        bank = quote(); bank['valid_for'] = '2026-10-12'
        self.assertEqual(source_quality(bank, 'Europe/Zagreb', NOW)['status'], 'ok')

    def test_future_date_is_quarantined_until_local_effective_day(self):
        bank = quote(); bank['valid_for'] = '2026-10-13'
        yesterday = dt.datetime(2026, 10, 12, 10, tzinfo=dt.timezone.utc)
        bank['fetched_at'] = yesterday.isoformat()
        result = source_quality(bank, 'Asia/Phnom_Penh', yesterday)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['freshness'], 'future')
        self.assertFalse(result['rates'])
        self.assertEqual(source_quality(bank, 'Asia/Phnom_Penh', NOW)['status'], 'ok')

    def test_future_published_time_is_rejected_even_if_day_is_today(self):
        bank = quote(); bank['published_at'] = '2026-10-13T19:00:00+07:00'
        self.assertEqual(source_quality(bank, 'Asia/Ho_Chi_Minh', NOW)['freshness'], 'future')

    def test_missing_or_invalid_date_does_not_use_fetch_date(self):
        for date in (None, '', 'bad', '2026-02-30'):
            bank = quote(); bank['valid_for'] = date
            result = source_quality(bank, 'UTC', NOW)
            self.assertEqual(result['freshness'], 'unknown')
            self.assertEqual(result['status'], 'stale')
            self.assertIsNone(result['source_date'])
            self.assertEqual(result['rates'], bank['rates'])

    def test_publication_uses_source_timezone_and_24_hour_age(self):
        bank = quote(); bank.pop('valid_for'); bank['published_at'] = '2026/10/13 12:00:00'
        result = source_quality(bank, 'Asia/Shanghai', NOW)
        self.assertEqual(result['freshness'], 'current')
        bank['published_at'] = '2026-10-12T03:00:00Z'
        self.assertEqual(source_quality(bank, 'Asia/Shanghai', NOW)['status'], 'stale')

    def test_aliases_and_retired_currencies_are_removed(self):
        bank = quote(); bank['rates'].update({code: {'middle': 1} for code in ['CNH','FOK','GGP','IMP','JEP','KID','TVD','HRK','BGN','ANG','SLL','ZWL']})
        result = source_quality(bank, 'UTC', NOW)
        self.assertEqual(set(result['rates']), {'USD'})
        self.assertEqual(len(result['excluded_currency_codes']), 12)
        self.assertTrue({'EUR','VND','XCG','SLE','ZWG'} <= ISO_CODES)

    def test_skipped_and_failed_sources_keep_their_status(self):
        for status in ('skipped', 'error'):
            bank = quote(); bank['status'] = status
            self.assertEqual(source_quality(bank, 'UTC', NOW), bank)

    def test_unchanged_countries_are_revalidated_without_rewriting_checks(self):
        bank = quote(); bank['valid_for'] = '2026-10-10'
        country = dict(checked_at='2026-10-10T05:00:00Z', central_bank=bank, banks=[])
        doc = dict(countries={'HR': country}, global_reference={})
        apply_quality(doc, {'countries': {'HR': {'tz': 'Europe/Zagreb'}}}, NOW)
        self.assertEqual(country['checked_at'], '2026-10-10T05:00:00Z')
        self.assertEqual(country['central_bank']['status'], 'stale')
        revision = country['quality_checked_at']
        apply_quality(doc, {'countries': {'HR': {'tz': 'Europe/Zagreb'}}}, NOW + dt.timedelta(minutes=1))
        self.assertEqual(country['quality_checked_at'], revision)

    def test_techcombank_naive_time_is_local_not_runner_utc(self):
        data = {'exchangeRate': {'data': [{'label':'EUR','sourceCurrency':'EUR','bidRateTM':29000,'bidRateCK':29000,'askRate':30000,'inputDate':'2026-10-13T12:02:16.359'}]}}
        with patch.object(fetch_rates, 'tcb_data', return_value=('https://example.test', data)):
            self.assertEqual(fetch_rates.techcombank()['published_at'], '2026-10-13T12:02:16.359000+07:00')


if __name__ == '__main__':
    unittest.main()
