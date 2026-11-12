import unittest
from pydantic import ValidationError
from mcp_backend.contracts import FindInput,TargetSelection
from mcp_backend.mutation_contracts import UpdateInput,SelectionMutation,HistoryInput,MUTATION_INPUTS


def update():
    return {'request_id':'mutation-test-001','sources':[{'ref':'s','text':'Change Alpha amount to 65.'}],
        'instruction_quotes':[{'source_ref':'s','quote':'Change Alpha amount to 65.'}],
        'changes':[{'action':'replace_fact','entity_id':'object-001','expected_version':1,'predicate':'amount',
            'fact_ids':['fact-001'],'quotes':[{'source_ref':'s','quote':'65'}],
            'replacements':[{'value':{'type':'number','decimal':'65','unit':''},'inherit_unit':True,
                'quotes':[{'source_ref':'s','quote':'65'}]}]}]}


class MutationContractTests(unittest.TestCase):
    def test_v2_and_strict_schema(self):
        value=UpdateInput.model_validate(update());self.assertEqual(value.schema_version,'2.0')
        for model in MUTATION_INPUTS.values():self.assertFalse(model.model_json_schema()['additionalProperties'])
        for extra in ('confirmed','approve','actor','dataset','cypher'):
            with self.subTest(extra=extra),self.assertRaises(ValidationError):UpdateInput.model_validate({**update(),extra:True})
        with self.assertRaises(ValidationError):UpdateInput.model_validate({**update(),'schema_version':'1.0'})

    def test_received_instruction_and_replacement_evidence(self):
        data=update();data['changes'][0]['replacements'][0]['quotes'][0]['quote']='fabricated'
        with self.assertRaises(ValidationError):UpdateInput.model_validate(data)

    def test_no_null_or_structural_entity_changes(self):
        for fields in ({},{'name':None},{'kind':'Other'},{'name':'x','deleted':True}):
            data=update();data['changes']=[{'action':'set_entity_fields','entity_id':'o','expected_version':1,
                'fields':fields,'quotes':[{'source_ref':'s','quote':'Alpha'}]}]
            with self.subTest(fields=fields),self.assertRaises(ValidationError):UpdateInput.model_validate(data)

    def test_inherited_unit_cannot_supply_new_unit(self):
        data=update();data['changes'][0]['replacements'][0]['value']['unit']='CNY'
        with self.assertRaises(ValidationError):UpdateInput.model_validate(data)

    def test_selection_is_typed_and_complete(self):
        self.assertEqual(FindInput(selection={'resource':'workspace'}).selection.resource,'workspace')
        for data in ({'resource':'workspace','ids':['o']},{'resource':'source','kind':'Client'},
                     {'resource':'entity','ids':['o','o']},{'resource':'entity','cypher':'MATCH (n)'},
                     {'resource':'entity','predicate':'amount'}):
            with self.subTest(data=data),self.assertRaises(ValidationError):TargetSelection.model_validate(data)
        body=update();body.pop('changes');body.update(selection={'resource':'workspace'},expected_revision=0,expected_count=12,selection_digest='a'*64)
        SelectionMutation.model_validate(body)
        body.pop('expected_count')
        with self.assertRaises(ValidationError):SelectionMutation.model_validate(body)

    def test_history_requires_one_bounded_target(self):
        HistoryInput(entity_id='o')
        for args in ({},{'entity_id':'o','source_id':'s'},{'entity_id':'o','limit':101}):
            with self.subTest(args=args),self.assertRaises(ValidationError):HistoryInput(**args)
