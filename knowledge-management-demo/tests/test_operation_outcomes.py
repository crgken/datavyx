from types import SimpleNamespace
import unittest
from pydantic_ai import ModelRetry
from demo.agent_runtime import validate_operation_outcome
from demo.contracts import RequestIntent,TurnOutput


class OperationOutcomeTests(unittest.TestCase):
    def deps(self,kind='change',active=None):
        store=SimpleNamespace(operation_context=lambda scope:{'active':active or []})
        return SimpleNamespace(intent=RequestIntent(kind=kind),approval_id='',store=store,scope=None,query_results={})

    def test_write_cannot_end_with_an_unverified_answer(self):
        for kind in ('record','change'):
            with self.subTest(kind=kind),self.assertRaises(ModelRetry):
                validate_operation_outcome(self.deps(kind),TurnOutput(action='answer',message='An old pending review covers this request.'))

    def test_query_cannot_replace_the_requested_effect(self):
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(self.deps(),TurnOutput(action='query',query_id='q'))

    def test_closed_operation_reference_is_rejected(self):
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(self.deps(),TurnOutput(action='review',draft_id='cancelled'))

    def test_operation_without_current_authorization_is_rejected(self):
        deps=self.deps(active=[{'draft_id':'p','ready_for_review':False}])
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(deps,TurnOutput(action='review',draft_id='p'))

    def test_current_authorized_operation_can_be_reviewed(self):
        deps=self.deps(active=[{'draft_id':'p','ready_for_review':True}])
        validate_operation_outcome(deps,TurnOutput(action='review',draft_id='p'))

    def test_no_change_requires_complete_empty_record_evidence(self):
        deps=self.deps()
        for result in ({},{'operation':'COUNT','complete':True,'matched_count':1},
                       {'operation':'LIST','complete':False,'matched_count':0},
                       {'operation':'SUM','complete':True,'matched_count':0}):
            deps.query_results['q']=result
            with self.subTest(result=result),self.assertRaises(ModelRetry):
                validate_operation_outcome(deps,TurnOutput(action='no_change',query_id='q'))
        deps.query_results['q']={'operation':'COUNT','complete':True,'matched_count':0}
        validate_operation_outcome(deps,TurnOutput(action='no_change',query_id='q'))

    def test_read_only_answer_is_unaffected(self):
        validate_operation_outcome(self.deps('read'),TurnOutput(action='answer',message='Explanation.'))

    def test_whole_scope_cannot_use_an_unverified_partial_preview(self):
        deps=self.deps()
        deps.intent=RequestIntent(kind='change',target_scope='workspace',operation='archive')
        deps.drafts={'partial':{'reviews':[{'target_id':'one'}]}}
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(deps,TurnOutput(action='preview',draft_id='partial'))

    def test_whole_scope_requires_the_current_frozen_set(self):
        deps=self.deps()
        deps.intent=RequestIntent(kind='change',target_scope='workspace',operation='archive')
        deps.operation_coverage={'target_ids':['one','two'],'revision':5}
        deps.drafts={'p':{'scope_coverage':{'target_ids':['one'],'revision':5}}}
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(deps,TurnOutput(action='preview',draft_id='p'))
        deps.drafts['p']['scope_coverage']=deps.operation_coverage
        validate_operation_outcome(deps,TurnOutput(action='preview',draft_id='p'))

    def test_whole_scope_cannot_use_an_unrelated_empty_query(self):
        deps=self.deps()
        deps.intent=RequestIntent(kind='change',target_scope='workspace',operation='archive')
        deps.query_results['q']={'operation':'LIST','complete':True,'matched_count':0}
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(deps,TurnOutput(action='no_change',query_id='q'))

    def test_unclear_scope_cannot_be_assumed_empty(self):
        deps=self.deps()
        deps.intent=RequestIntent(kind='change',target_scope='unclear',operation='archive')
        deps.query_results['q']={'operation':'COUNT','complete':True,'matched_count':0}
        with self.assertRaises(ModelRetry):
            validate_operation_outcome(deps,TurnOutput(action='no_change',query_id='q'))

    def test_completed_confirmation_keeps_its_receipt_answer(self):
        deps=self.deps()
        deps.approval_id='server-approved'
        validate_operation_outcome(deps,TurnOutput(action='answer',message='Completed.'))


if __name__=='__main__':
    unittest.main()
