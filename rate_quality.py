"""Offline source-date and ISO 4217 validation; retrieval is not publication."""
import datetime as dt
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ISO_CODES = frozenset(json.loads((ROOT / 'iso_4217.json').read_text())['codes'])
DAY = dt.timedelta(hours=24)
FUTURE_TOLERANCE = dt.timedelta(minutes=5)


def publication_time(value, tz):
    """Parse a declared source date, including legacy local bank timestamps."""
    if not value:
        return None, None
    text = str(value).strip().replace('/', '-')
    # CFETS sometimes returns a date plus an unpadded hour (YYYY-MM-DD 9:15).
    text = re.sub(r'^(\d{4}-\d{2}-\d{2}) (\d):', r'\1 0\2:', text)
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            day = dt.date.fromisoformat(text)
            return dt.datetime.combine(day, dt.time.min, tzinfo=tz), 'day'
        stamp = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=tz), 'time'
    except ValueError:
        return None, None


def source_quality(source, timezone='UTC', now=None):
    """Preserve all rates/dates on stale or unknown data; quarantine future data."""
    out = dict(source)
    if out.get('status') not in ('ok', 'stale'):
        return out
    now = now or dt.datetime.now(dt.timezone.utc)
    tz = ZoneInfo(out.get('source_timezone') or timezone)
    out['source_timezone'] = str(tz)
    for key in ('rates', 'rates_per_eur'):
        if key in out:
            removed = sorted(set(out[key]) - ISO_CODES)
            if removed:
                out['excluded_currency_codes'] = sorted(set(out.get('excluded_currency_codes', [])) | set(removed))
            out[key] = {k: v for k, v in out[key].items() if k in ISO_CODES}
            out['count'] = len(out[key])
    valid, precision = publication_time(out.get('valid_for'), tz)
    published, pub_precision = publication_time(out.get('published_at'), tz)
    fetched, _ = publication_time(out.get('fetched_at'), ZoneInfo('UTC'))
    out['date_precision'] = precision or pub_precision
    declared = valid or published
    out['source_date'] = declared.astimezone(tz).date().isoformat() if declared else None
    future = ((valid and (valid.date() > now.astimezone(tz).date() if precision == 'day' else valid > now + FUTURE_TOLERANCE))
              or (published and (published.date() > now.astimezone(tz).date() if pub_precision == 'day' else published > now + FUTURE_TOLERANCE))
              or (fetched and fetched > now + FUTURE_TOLERANCE))
    if future:
        out.update(status='error', freshness='future', stale_reason='future_date', error='Source date is in the future; rates are not yet applicable.', count=0)
        out['rates'] = {}
        out.pop('rates_per_eur', None)
        return out
    # A day-only list remains usable through the following calendar day. Its
    # precise publication time is unknown; never invent midnight as that time.
    old = (valid and ((now.astimezone(tz).date() - valid.date()).days > 1 if precision == 'day' else now - valid > DAY))
    if published:
        old = old or ((now.astimezone(tz).date() - published.astimezone(tz).date()).days > 1 if pub_precision == 'day' else now - published > DAY)
    if fetched:
        old = old or now - fetched > DAY
    out['freshness'] = 'unknown' if not declared else 'stale' if old else 'current'
    if out['freshness'] != 'current':
        out['status'] = 'stale'
        out['stale_reason'] = 'missing_source_date' if not declared else 'old_source_date'
        out['stale_since'] = out.get('stale_since') or now.replace(microsecond=0).isoformat()
    if not (out.get('rates') or out.get('rates_per_eur')):
        out.update(status='error', error='No supported ISO 4217 rates.', count=0)
    return out


def apply_quality(doc, schedule, now=None):
    """Recheck unchanged and merged documents, keeping retrieval dates intact."""
    now = now or dt.datetime.now(dt.timezone.utc)
    checked = now.replace(microsecond=0).isoformat()
    for cc, country in doc.get('countries', {}).items():
        timezone = schedule.get('countries', {}).get(cc, {}).get('tz', 'UTC')
        changed = False
        for field in ('central_bank', 'banks'):
            before = country[field]
            after = ([source_quality(s, timezone, now) for s in before] if field == 'banks'
                     else source_quality(before, timezone, now))
            changed = changed or after != before
            country[field] = after
        if changed:
            country['quality_checked_at'] = checked
    before = doc.get('global_reference', {})
    after = {k: source_quality(s, 'UTC', now) for k, s in before.items()}
    if before != after:
        doc['global_quality_checked_at'] = checked
    doc['global_reference'] = after
    return doc
