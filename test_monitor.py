import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from monitor import GitHub, changes, discord_sender, main, ntfy_sender, run


def snapshot(kind="Issue"):
    return dict(title="An issue", state="open", labels=["not-approved"],
                assignees=[], comments=0, updated_at="2026-10-27T01:00:00Z", kind=kind)


class MonitorTests(unittest.TestCase):
    def test_first_run_is_silent_and_second_run_notifies_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            client, send = Mock(), Mock()
            client.contributions.return_value = [{"html_url": "https://github.com/example/project/issues/1"}]
            client.snapshot.return_value = snapshot()
            run(client, send, path)
            send.assert_not_called()
            client.snapshot.return_value = dict(snapshot(), labels=["approved"])
            run(client, send, path)
            self.assertEqual(send.call_args.kwargs, {"kind": "Issue"})
            self.assertIn("Labels: approved", send.call_args.args[1])
            send.reset_mock()
            run(client, send, path)
            send.assert_not_called()

    def test_pr_notifications_use_pr_route(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            client, send = Mock(), Mock()
            client.contributions.return_value = [{"html_url": "https://github.com/example/project/pull/1"}]
            client.snapshot.return_value = snapshot("PR")
            run(client, send, path)
            client.snapshot.return_value = dict(snapshot("PR"), state="merged")
            run(client, send, path)
            self.assertEqual(send.call_args.kwargs, {"kind": "PR"})
            self.assertIn("State: merged", send.call_args.args[1])

    def test_delivery_failure_keeps_previous_snapshot_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            client, send = Mock(), Mock()
            client.contributions.return_value = [{"html_url": "https://github.com/example/project/issues/1"}]
            client.snapshot.return_value = snapshot()
            run(client, send, path)
            before = path.read_text()
            client.snapshot.return_value = dict(snapshot(), comments=1)
            send.side_effect = RuntimeError("Discord unavailable")
            with self.assertRaises(RuntimeError):
                run(client, send, path)
            self.assertEqual(path.read_text(), before)

    def test_checks_reviews_remain_equal_after_state_roundtrip(self):
        client = GitHub("unused")
        client.get = Mock(side_effect=[
            {"merged_at": None, "state": "open", "draft": False,
             "head": {"sha": "abc"}, "review_comments": 0},
            {"statuses": [{"context": "build", "state": "success"}]},
        ])
        client.pages = Mock(side_effect=[
            [{"id": 1, "state": "APPROVED", "submitted_at": "2026-10-27"}],
            [{"app": {"id": 1}, "name": "test", "id": 2, "conclusion": "success"}],
        ])
        item = dict(title="PR", state="open", labels=[], assignees=[], comments=0,
                    updated_at="now", pull_request={}, number=1,
                    repository_url="https://api.github.com/repos/example/project")
        new = client.snapshot(item)
        self.assertEqual(changes(json.loads(json.dumps(new)), new), [])

    def test_webhook_blocks_unintended_mentions(self):
        with patch("monitor.request_json") as request:
            send = discord_sender("https://discord.com/api/webhooks/123/fake-test-token", "456")
            send("@everyone", "@here")
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["allowed_mentions"], {"parse": [], "roles": ["456"]})
            self.assertEqual(payload["content"], "<@&456>")

    def test_issue_and_pr_pings_allow_only_their_designated_role(self):
        with patch("monitor.request_json") as request:
            for role_id in ("1564451806325768364", "1564451861111635978"):
                sender = discord_sender("https://discord.com/api/webhooks/123/fake-test-token", role_id)
                sender("An update", "@everyone <@&999>")
                payload = request.call_args.kwargs["payload"]
                self.assertEqual(payload["allowed_mentions"], {"parse": [], "roles": [role_id]})
                self.assertEqual(payload["content"], f"<@&{role_id}>")

    def test_main_routes_real_run_calls_to_both_webhooks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            client = Mock()
            client.contributions.return_value = [
                {"html_url": "https://github.com/example/project/issues/1"},
                {"html_url": "https://github.com/example/project/pull/2"},
            ]
            client.snapshot.side_effect = [
                snapshot(), snapshot("PR"),
                dict(snapshot(), comments=1), dict(snapshot("PR"), state="merged"),
            ]
            environment = {
                "ISSUES_WEBHOOK_URL": "https://discord.com/api/webhooks/123/fake-issues-token",
                "PRS_WEBHOOK_URL": "https://discord.com/api/webhooks/789/fake-prs-token",
                "ISSUES_ROLE_ID": "456", "PRS_ROLE_ID": "987",
            }
            with patch.dict("os.environ", environment), \
                    patch("sys.argv", ["monitor.py", "--state", str(path)]), \
                    patch("monitor.GitHub", return_value=client), \
                    patch("monitor.request_json") as request:
                main()
                request.assert_not_called()
                main()
                self.assertEqual(request.call_count, 2)
                issue_call, pr_call = request.call_args_list
                self.assertEqual(issue_call.args[0], environment["ISSUES_WEBHOOK_URL"] + "?wait=true")
                self.assertEqual(pr_call.args[0], environment["PRS_WEBHOOK_URL"] + "?wait=true")
                self.assertEqual(issue_call.kwargs["payload"]["content"], "<@&456>")
                self.assertEqual(pr_call.kwargs["payload"]["content"], "<@&987>")

    def test_ntfy_sends_high_priority_json_to_root_endpoint(self):
        with patch("monitor.request_json") as request:
            send = ntfy_sender("a-long-random-topic-for-tests")
            send("PR updated", "New review", "https://github.com/example/project/pull/1", kind="PR")
            self.assertEqual(request.call_args.args[0], "https://ntfy.sh/")
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["priority"], 4)
            self.assertEqual(payload["click"], "https://github.com/example/project/pull/1")
            self.assertEqual(payload["topic"], "a-long-random-topic-for-tests")

    def test_ntfy_mode_works_without_discord_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ntfy-snapshot.json"
            client = Mock()
            client.contributions.return_value = [{"html_url": "https://github.com/example/project/issues/1"}]
            client.snapshot.side_effect = [snapshot(), dict(snapshot(), labels=["approved"])]
            with patch.dict("os.environ", {"NTFY_TOPIC": "a-long-random-topic-for-tests"}, clear=True), \
                    patch("sys.argv", ["monitor.py", "--ntfy-only", "--state", str(path)]), \
                    patch("monitor.GitHub", return_value=client), \
                    patch("monitor.request_json") as request:
                main()
                request.assert_not_called()
                main()
                request.assert_called_once()
                self.assertIn("Labels: approved", request.call_args.kwargs["payload"]["message"])

    def test_ntfy_failure_does_not_modify_discord_state(self):
        with tempfile.TemporaryDirectory() as directory:
            discord_path = Path(directory) / "snapshot.json"
            ntfy_path = Path(directory) / "ntfy-snapshot.json"
            client = Mock()
            client.contributions.return_value = [{"html_url": "https://github.com/example/project/issues/1"}]
            client.snapshot.return_value = snapshot()
            run(client, Mock(), discord_path)
            run(client, Mock(), ntfy_path)
            client.snapshot.return_value = dict(snapshot(), comments=1)
            run(client, Mock(), discord_path)
            discord_saved = discord_path.read_text()
            failing_ntfy = Mock(side_effect=RuntimeError("offline"))
            with self.assertRaises(RuntimeError):
                run(client, failing_ntfy, ntfy_path)
            self.assertEqual(discord_path.read_text(), discord_saved)
            successful_ntfy = Mock()
            run(client, successful_ntfy, ntfy_path)
            successful_ntfy.assert_called_once()

    def test_rejects_non_discord_webhook(self):
        with self.assertRaises(ValueError):
            discord_sender("https://example.com/webhook", "456")


if __name__ == "__main__":
    unittest.main()
