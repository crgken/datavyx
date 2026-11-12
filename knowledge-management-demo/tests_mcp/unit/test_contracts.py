import copy
import unittest
from pydantic import ValidationError
from mcp_backend.contracts import RecordInput, QueryInput, ContextInput, SourcesInput, TOOL_INPUTS, TOOL_OUTPUTS


def record():
    return {"request_id": "request-test-001", "sources": [{"ref": "s1", "text": "Alpha is in Chicago."}],
            "entities": [{"ref": "a", "action": "create", "kind": "Company", "name": "Alpha",
                          "quotes": [{"source_ref": "s1", "quote": "Alpha"}]}],
            "facts": [{"subject_ref": "a", "predicate": "city", "value": {"type": "text", "text": "Chicago"},
                       "quotes": [{"source_ref": "s1", "quote": "Chicago"}]}]}


class ContractTests(unittest.TestCase):
    def test_record_round_trip(self):
        data = RecordInput.model_validate(record())
        self.assertEqual(data, RecordInput.model_validate_json(data.model_dump_json()))

    def test_no_identity_or_execution_overrides(self):
        for field in ("actor", "dataset", "cypher", "confirmed", "approval_id", "model"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RecordInput.model_validate({**record(), field: "untrusted"})

    def test_reference_cannot_change_fields(self):
        data = record()
        data['entities'] = [{'ref':'a', 'action':'reference', 'entity_id':'actual-id', 'name':'overwrite'}]
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)

    def test_duplicate_and_dangling_refs(self):
        for mutation in (lambda d:d['entities'].append(copy.deepcopy(d['entities'][0])),
                         lambda d:d['facts'][0].update(subject_ref='missing'),
                         lambda d:d['facts'][0]['quotes'][0].update(source_ref='missing')):
            data=record();mutation(data)
            with self.assertRaises(ValidationError): RecordInput.model_validate(data)

    def test_value_relation_exclusive(self):
        data=record();data['facts'][0]['object_ref']='a'
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)

    def test_source_only_is_recorded_without_fake_entities(self):
        data=record();data.update(recording_mode='source_only')
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)
        data.update(entities=[],facts=[])
        self.assertEqual(RecordInput.model_validate(data).recording_mode,'source_only')

    def test_unknown_version_and_action(self):
        data=record();data['schema_version']='2.0'
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)
        data=record();data['entities'][0]['action']='update'
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)

    def test_limits_are_strict(self):
        for limit in (True,'25',0,101):
            with self.subTest(limit=limit),self.assertRaises(ValidationError): ContextInput(limit=limit)
        data=record();data['sources'][0]['text']='x'*16001
        with self.assertRaises(ValidationError): RecordInput.model_validate(data)

    def test_numbers_are_decimal_strings_and_preserve_ids(self):
        for value in (12.3, '1e3', '1.1234567'):
            data=record();data['facts'][0]['value']={'type':'number','decimal':value,'unit':'USD'}
            with self.subTest(value=value),self.assertRaises(ValidationError): RecordInput.model_validate(data)
        data=record();data['entities'][0]['identifier']='000123'
        self.assertEqual(RecordInput.model_validate(data).entities[0].identifier,'000123')

    def test_queries_require_precise_aggregation(self):
        for data in ({'operation':'SUM'}, {'operation':'SUM','predicate':'amount'},
                     {'operation':'SUM','predicate':'amount','unit':'USD','direction':'in'},
                     {'operation':'COUNT','filters':[{'predicate':'x','operator':'exists','value':{'type':'text','text':'a'}}]}):
            with self.subTest(data=data),self.assertRaises(ValidationError): QueryInput.model_validate(data)
        self.assertEqual(QueryInput(operation='SUM',predicate='amount',unit='USD').unit,'USD')

    def test_source_target_exclusive(self):
        for data in ({},{'source_id':'s','fact_id':'f'}):
            with self.assertRaises(ValidationError):SourcesInput.model_validate(data)

    def test_tools_and_schema_are_bounded(self):
        self.assertEqual(len(TOOL_INPUTS),7)
        self.assertEqual(TOOL_INPUTS.keys(),TOOL_OUTPUTS.keys())
        for name,model in TOOL_INPUTS.items():
            self.assertEqual(model.model_json_schema()['additionalProperties'],False,name)
        self.assertNotIn('confirm',TOOL_INPUTS)


if __name__ == '__main__': unittest.main()
