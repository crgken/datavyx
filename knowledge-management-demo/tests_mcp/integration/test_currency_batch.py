"""Synthetic twelve-transaction regression; never writes the user's workspace."""
from decimal import Decimal
import unittest
from mcp_backend.contracts import QueryInput, RecordInput, ReceiptInput, SourcesInput
from mcp_backend.errors import KnowledgeError
from mcp_backend.ingestion import Ingestion
from mcp_backend.neo4j_repository import Repository
from mcp_backend.query import Queries
from mcp_backend.records import Records
from tests_mcp.support import test_settings, workspace, cleanup


TRANSACTIONS = [
    (1,'income','工资',10000,'工资'), (1,'expense','住房',300,'物业费'),
    (2,'expense','交通',80,'打车'), (2,'expense','餐饮',45,'午餐'),
    (3,'expense','餐饮',120,'晚餐'), (3,'expense','交通',60,'公交卡充值'),
    (4,'income','兼职',2000,'兼职'), (4,'expense','购物',200,'购买衣服'),
    (4,'expense','购物',150,'购买日用品'), (5,'expense','通信',90,'手机话费'),
    (5,'expense','住房',500,'房屋维修'), (6,'expense','餐饮',55,'晚餐')]


def batch():
    header='模拟个人收支，全部为2026年11月已实际发生的交易，币种为人民币。'
    data={'request_id':'currency-batch-001','sources':[],'entities':[],'facts':[]}
    lines=[]
    for i,(day,direction,category,amount,note) in enumerate(TRANSACTIONS,1):
        ref=f't{i:03}'; identifier=f'{i:03}'
        line=f'{identifier}：11月{day}日，{category}{"收入" if direction=="income" else "支出"}{amount}元，用于{note}。'
        lines.append(line); quote={'source_ref':'s','quote':line}
        data['entities'].append({'ref':ref,'action':'create','kind':'transaction','identifier':identifier,
            'namespace':'synthetic-ledger-2026-11','quotes':[quote]})
        for predicate,value in [('direction',direction),('category',category),('date',f'2026-11-{day:02}')]:
            data['facts'].append({'subject_ref':ref,'predicate':predicate,
                'value':{'type':'text','text':value},'quotes':[quote]})
        data['facts'].append({'subject_ref':ref,'predicate':'amount',
            'value':{'type':'number','decimal':str(amount),'unit':'CNY',
                'unit_quotes':[{'source_ref':'s','quote':'人民币'}]},
            'quotes':[quote,{'source_ref':'s','quote':header}]})
    data['sources']=[{'ref':'s','text':header+'\n'+'\n'.join(lines)}]
    return data


def text_filter(predicate,value):
    return {'predicate':predicate,'operator':'eq','value':{'type':'text','text':value}}


class CurrencyBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate()
        cls.ingestion=Ingestion(cls.repo);cls.queries=Queries(cls.repo);cls.records=Records(cls.repo)

    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):self.scope=workspace(self.repo)
    def tearDown(self):cleanup(self.repo,self.scope)
    def save(self,data):return self.ingestion.record_information(self.scope,RecordInput.model_validate(data))
    def query(self,operation='COUNT',**kwargs):
        return self.queries.query_records(self.scope,QueryInput(operation=operation,kind='transaction',**kwargs))
    def total(self,filters):
        result=self.query('SUM',predicate='amount',unit='CNY',filters=filters)
        self.assertTrue(result['aggregate_complete'])
        return Decimal(result['sum'])

    def test_twelve_records_receipt_source_and_exact_totals(self):
        data=batch();saved=self.save(data)
        self.assertEqual((saved['objects_created'],saved['facts_created'],saved['sources_recorded']),(12,48,1))
        income=[text_filter('direction','income')];expense=[text_filter('direction','expense')]
        self.assertEqual(self.query(limit=1)['count'],12)
        self.assertEqual(self.query(filters=income)['count'],2)
        self.assertEqual(self.query(filters=expense)['count'],10)
        self.assertEqual((self.total(income),self.total(expense)),(12000,1600))
        self.assertEqual(self.total(income)-self.total(expense),10400)
        for category,count,amount in [('住房',2,800),('购物',2,350),('餐饮',3,220),('交通',2,140),('通信',1,90)]:
            filters=expense+[text_filter('category',category)]
            self.assertEqual(self.query(filters=filters)['count'],count)
            self.assertEqual(self.total(filters),amount)
        for day,amount in enumerate([300,125,180,350,590,55],1):
            self.assertEqual(self.total(expense+[text_filter('date',f'2026-11-{day:02}')]),amount)
        over=expense+[{'predicate':'amount','operator':'gt','value':{'type':'number','decimal':'100','unit':'CNY'}}]
        self.assertEqual(self.query(filters=over)['count'],5);self.assertEqual(self.total(over),1270)
        maximum=self.query('LIST',filters=expense+[{'predicate':'amount','operator':'gte',
            'value':{'type':'number','decimal':'500','unit':'CNY'}}])
        self.assertEqual([r['identifier'] for r in maximum['rows']],['011'])
        source=self.records.get_sources(self.scope,SourcesInput(source_id=saved['source_ids']['s']))
        self.assertEqual(source['rows'][0]['text'],data['sources'][0]['text'])
        again=self.save(data);self.assertTrue(again['idempotent']);self.assertEqual(self.query()['count'],12)
        receipt=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=data['request_id']))
        self.assertEqual(receipt['receipt']['receipt_id'],saved['receipt_id'])

    def test_bad_last_currency_quote_leaves_no_partial_batch_or_receipt(self):
        data=batch();data['facts'][-1]['value']['unit']='JPY'
        with self.assertRaises(KnowledgeError):self.save(data)
        self.assertEqual(self.query()['count'],0)
        receipt=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=data['request_id']))
        self.assertFalse(receipt['found'])
        counts=self.repo.transaction(lambda tx:tx.run('MATCH (n {dataset:$d}) WHERE n:KDObject OR n:KDFact OR n:KDSource OR n:KDMCPRequest RETURN count(n) AS n',d=self.scope.dataset).single()['n'])
        self.assertEqual(counts,0)
