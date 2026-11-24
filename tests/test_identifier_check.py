"""Belirsiz numaranın macOS dışındaki platformlarda tahminle doldurulmadığını sınar."""
import unittest
from unittest.mock import patch
from fatura.extraction.identifier_check import check_identifier


class IdentifierCheckTests(unittest.TestCase):
    def test_missing_prefix_is_not_replaced_by_a_guessed_letter(self):
        class Backend:
            name='tesseract'
        crops={'regions':{'invoice_number':[{'text':'102026001','confidence':.9}]},
               'identifier_attempts':{'invoice_number':[[{'text':'$102026001'}]]}}
        with patch('fatura.extraction.identifier_check.sys.platform','win32'):
            check_identifier('unused',{'invoice_number':[0,0,1,1]},crops,Backend())
        self.assertTrue(crops['identifier_unresolved'])
        self.assertTrue(crops['identifier_review'])
        self.assertEqual(crops['regions']['invoice_number'][0]['text'],'102026001')
