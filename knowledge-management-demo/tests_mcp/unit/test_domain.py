import unittest
from mcp_backend.contracts import RecordInput,NumberValue
from mcp_backend.domain import digest,identity_key,validate_record,number_fields,eligibility
from mcp_backend.errors import KnowledgeError
from mcp_backend.unit_labels import unit_label_matches
from test_contracts import record


class DomainTests(unittest.TestCase):
    def test_identity_is_explicit_and_preserves_significant_punctuation(self):
        self.assertNotEqual(identity_key('Company','','001'),identity_key('Company','','1'))
        self.assertNotEqual(identity_key('Company','','A-B'),identity_key('Company','','AB'))
        self.assertEqual(identity_key('Company','',''), '')
        self.assertEqual(identity_key('Company','us',' X '),identity_key('company','US','x'))

    def test_quotes_are_checked_against_received_text(self):
        data=RecordInput.model_validate(record());validate_record(data)
        data.facts[0].quotes[0].quote='Seattle'
        with self.assertRaises(KnowledgeError):validate_record(data)

    def test_decimal_is_exact(self):
        value,scaled=number_fields(NumberValue(type='number',decimal='0.100000',unit=''))
        self.assertEqual((value['value'],scaled),('0.1',100000))
        with self.assertRaises(KnowledgeError):number_fields(NumberValue(type='number',decimal='1000000001',unit=''))

    def test_units_need_explicit_matching_evidence(self):
        d=record();d['facts'][0]['value']={'type':'number','decimal':'100','unit':'USD'}
        with self.assertRaises(KnowledgeError):validate_record(RecordInput.model_validate(d))
        d['sources'][0]['text']='Alpha has 100 USD.'
        d['facts'][0]['quotes']=[{'source_ref':'s1','quote':'100 USD'}]
        d['facts'][0]['value']['unit_quotes']=[{'source_ref':'s1','quote':'USD'}]
        validate_record(RecordInput.model_validate(d))
        d['sources'][0]['text']='Alpha has 100 USDC.'
        d['facts'][0]['quotes'][0]['quote']='100 USDC'
        d['facts'][0]['value']['unit_quotes'][0]['quote']='USDC'
        with self.assertRaises(KnowledgeError):validate_record(RecordInput.model_validate(d))

    def test_conflict_is_order_independent_and_does_not_hide_objects(self):
        objects={'o':{'id':'o'}}
        facts=[{'id':str(i),'subject_id':'o','predicate':'city','value_json':'{"type":"text","value":"'+v+'"}'} for i,v in enumerate(['Chicago','Seattle'])]
        self.assertEqual(eligibility(objects,facts),eligibility(objects,facts[::-1]))
        self.assertTrue(all('conflict' in r for r in eligibility(objects,facts).values()))
        self.assertEqual(len(objects),1)

    def test_currency_names_require_unique_full_labels(self):
        for literal, unit in [('人民币','CNY'),('人民幣','CNY'),('US dollars','USD'),('日元','JPY'),('kg','kg')]:
            with self.subTest(literal=literal):self.assertTrue(unit_label_matches(literal,unit))
        for literal, unit in [('元','CNY'),('$','USD'),('¥','CNY'),('USDC','USD'),('人民币','JPY'),('币种为人民币','CNY')]:
            with self.subTest(literal=literal):self.assertFalse(unit_label_matches(literal,unit))

    def test_shared_currency_statement_must_be_quoted_for_this_fact(self):
        d=record();d['sources'][0]['text']='币种为人民币。Alpha 支出100元。'
        d['facts'][0]['value']={'type':'number','decimal':'100','unit':'CNY',
            'unit_quotes':[{'source_ref':'s1','quote':'人民币'}]}
        d['facts'][0]['quotes']=[{'source_ref':'s1','quote':'Alpha 支出100元。'}]
        with self.assertRaises(KnowledgeError):validate_record(RecordInput.model_validate(d))
        d['facts'][0]['quotes'].append({'source_ref':'s1','quote':'币种为人民币。'})
        validate_record(RecordInput.model_validate(d))
        d['facts'][0]['value']['unit_quotes'][0]['quote']='Chinese Yuan'
        with self.assertRaises(KnowledgeError):validate_record(RecordInput.model_validate(d))

    def test_estimated_and_multivalue_are_not_mixed(self):
        objects={'o':{'id':'o'}}
        facts=[{'id':str(i),'subject_id':'o','predicate':'tag','cardinality':'many','value_json':'{"type":"text","value":"'+str(i)+'"}'} for i in range(2)]
        self.assertTrue(all(not r for r in eligibility(objects,facts).values()))
        facts[0]['certainty']='estimated'
        self.assertEqual(eligibility(objects,facts)['0'],['uncertain'])


if __name__=='__main__':unittest.main()
