"""Onaylı faturalar için para birimlerini karıştırmadan Decimal özetler üretir."""
from datetime import date
from decimal import Decimal, localcontext


def summarize(documents, currency, period='all', today=None):
    today = today or date.today()
    start = date(today.year, today.month if period == 'month' else 1, 1).isoformat()
    selected = [d for d in documents if period == 'all' or (d['invoice_date'] and start <= d['invoice_date'] <= today.isoformat())]
    approved = [d for d in selected if d['status'] == 'approved' and d['currency'] == currency]
    def amount(doc, key):
        return Decimal(doc['totals'].get(key) or '0')
    with localcontext() as ctx:
        ctx.prec = 50
        net, vat, gross = [sum((amount(d, key) for d in approved), Decimal(0)) for key in ('net', 'vat', 'gross')]
        months = []
        for offset in range(5, -1, -1):
            index = today.year * 12 + today.month - 1 - offset
            year, month = divmod(index, 12)
            key = f'{year}-{month+1:02}'
            matching = [d for d in approved if d['invoice_date'] and d['invoice_date'].startswith(key)]
            totals = {k: str(sum((amount(d, k) for d in matching), Decimal(0))) for k in ('net', 'vat', 'gross')}
            months.append({'key': key, **totals})
        vendors = {}
        for doc in approved:
            vendor = vendors.setdefault(doc['seller'] or 'Satıcı', {'count': 0, 'total': Decimal(0)})
            vendor['count'] += 1
            vendor['total'] += amount(doc, 'gross')
        top = sorted(vendors.items(), key=lambda row: row[1]['total'], reverse=True)[:5]
        return {'net': str(net), 'vat': str(vat), 'gross': str(gross), 'count': len(approved),
                'pending': len([d for d in selected if d['status'] != 'approved']), 'suppliers': len(vendors),
                'average': str((gross / len(approved)).quantize(Decimal('.01'))) if approved else '0',
                'vat_share': str((vat / (net + vat) * 100).quantize(Decimal('.1'))) if net + vat else '0',
                'months': months, 'vendors': [{'name': name, 'count': v['count'], 'total': str(v['total'])} for name, v in top]}
