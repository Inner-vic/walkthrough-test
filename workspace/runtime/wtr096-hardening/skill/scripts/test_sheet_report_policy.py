"""Each online detail has independent resource governance checkpoints."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import report_policy as policy
from request_model import InputError


class SheetPolicyTests(unittest.TestCase):
    def test_type_and_identity_match_sheet(self):
        settings={"report_lark_profile":"demo-bot"}
        calls=[]
        with patch.object(policy,"run_lark",side_effect=lambda settings,args,**kw:calls.append((args,kw))):
            url="https://example.feishu.cn/sheets/SyntheticSheet"
            policy.restrict_collaborator_management(settings,url)
            policy.transfer_report_owner(settings,url,"ou_requester")
            policy.revoke_secure_label_operator(settings,url,"ou_operator")
        for args,kwargs in calls:
            self.assertEqual(kwargs["profile"],"demo-bot")
            if "--type" in args:self.assertEqual(args[args.index("--type")+1],"sheet")
            if "--params" in args:self.assertEqual(json.loads(args[args.index("--params")+1])["type"],"sheet")

    def test_label_persist_callback_does_not_replace_main_permissions(self):
        settings={}
        record={"report_secure_label_required":True,"report_operator_open_id":"ou_requester",
                "sender_id":"ou_requester","report_permissions":{"main":"preserved"},
                "report_secure_label":{"id":"label","name":"L3"}}
        perms={}
        saved=[]
        with patch.object(policy,"apply_report_secure_label"),patch.object(policy,"atomic_json",side_effect=AssertionError("wrong checkpoint")):
            policy.configure_report_secure_label(settings,Path("unused"),record,
                "https://example.feishu.cn/sheets/Synthetic",perms,
                persist_callback=lambda:saved.append(dict(perms)))
        self.assertTrue(saved)
        self.assertIn("secure_label",perms)
        self.assertEqual(record["report_permissions"],{"main":"preserved"})

    def test_non_feishu_or_wrong_resource_urls_rejected(self):
        for url in ("https://evil.test/docx/X","https://example.feishu.cn/file/X","http://example.feishu.cn/sheets/X"):
            with self.subTest(url=url),self.assertRaises(InputError):policy._resource_identity(url)


if __name__=="__main__":unittest.main()
