"""Analizlerin onay durumunu, dönemi, para birimini ve hassas aritmetiği sınar."""
from datetime import date
import unittest
from fatura.analysis.analytics import summarize


def doc(amount, status='approved', currency='TRY', when='2026-11-14'):
    return {'status':status,'currency':currency,'invoice_date':when,'seller':'Örnek',
            'totals':{'net':amount,'vat':'0','gross':amount}}


class AnalyticsTests(unittest.TestCase):
    def test_decimal_sum_excludes_drafts_and_other_currencies(self):
        data=[doc('0.1'),doc('0.2'),doc('100','review'),doc('50',currency='EUR')]
        result=summarize(data,'TRY',today=date(2026,11,14))
        self.assertEqual(result['gross'],'0.3')
        self.assertEqual(result['count'],2)
        self.assertEqual(result['pending'],1)
        self.assertEqual(result['average'],'0.15')

    def test_month_period_excludes_previous_month_and_future(self):
        data=[doc('10'),doc('20',when='2026-10-10'),doc('50',when='2026-11-20')]
        result=summarize(data,'TRY','month',date(2026,11,14))
        self.assertEqual(result['gross'],'10')
        self.assertEqual(result['months'][-1]['gross'],'10')
