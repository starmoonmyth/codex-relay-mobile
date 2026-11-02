import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from backend import Chat
from claude_backend import ClaudeData, deliver_claude, _prompt_text


class ClaudeBackendTests(unittest.TestCase):
    def test_receipt_matches_cli_prompt_with_attachment_note(self):
        data = ClaudeData(pathlib.Path(tempfile.mkdtemp()))
        receipt = {
            "id": "request-id",
            "text": "看图",
            "images": [{"id": "upload-id"}],
            "baseline_ids": [],
        }
        message = type("Message", (), {"role": "user", "id": "message-id", "text":
            "看图\n\n[手机图片附件]\n请使用 Claude Code 的图片读取能力查看以下附件，再完成上面的任务：\n- C:\\uploads\\upload-id.png"})()
        self.assertEqual(_prompt_text(message.text), "看图")
        self.assertIs(data.receipt_message(receipt, [message]), message)

    def test_receipt_matches_image_only_cli_prompt(self):
        data = ClaudeData(pathlib.Path(tempfile.mkdtemp()))
        receipt = {"id": "request-id", "text": "", "images": [{"id": "upload-id"}], "baseline_ids": []}
        message = type("Message", (), {"role": "user", "id": "message-id", "text":
            "\n\n[手机图片附件]\n请使用 Claude Code 的图片读取能力查看以下附件，再完成上面的任务：\n- C:\\uploads\\upload-id.png"})()
        self.assertIs(data.receipt_message(receipt, [message]), message)

    def test_completed_receipt_is_not_left_in_pending_state(self):
        data = ClaudeData(pathlib.Path(tempfile.mkdtemp()))
        receipt = {
            "id": "request-id",
            "thread_id": "claude:session-id",
            "text": "看图",
            "images": [{"id": "upload-id"}],
            "baseline_ids": [],
            "result": "sent",
        }
        user = type("Message", (), {"role": "user", "id": "message-id", "text":
            "看图\n\n[手机图片附件]\n- C:\\uploads\\upload-id.png", "ordinal": 1, "turn_id": ""})()
        assistant = type("Message", (), {"role": "assistant", "id": "assistant-id", "text":
            "已处理", "ordinal": 2, "turn_id": ""})()
        self.assertEqual(data.receipt_status(receipt, [user, assistant], set()), "已完成")

    def test_reads_sessions_and_deduplicates_copied_transcripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            project = root / "projects" / "C--work"
            project.mkdir(parents=True)
            entries = [
                {"type": "custom-title", "customTitle": "原生 Claude 会话", "sessionId": "same"},
                {"type": "user", "sessionId": "same", "cwd": str(root),
                 "message": {"role": "user", "content": [{"type": "text", "text": "检查图片"}]}},
                {"type": "assistant", "sessionId": "same", "message": {"role": "assistant", "model": "claude-opus-fixture", "content": [{"type": "text", "text": "已检查"}]}},
                {"type": "assistant", "isSidechain": True, "sessionId": "same", "message": {"role": "assistant", "model": "claude-haiku-fixture", "content": []}},
            ]
            first = project / "same.jsonl"
            first.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n", encoding="utf-8")
            copy = project / "copy.jsonl"
            copy.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
            data = ClaudeData(root)
            chats = data.chats()
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].provider, "claude")
            self.assertEqual(data.history(chats[0])[-1].text, "已检查")
            self.assertEqual(chats[0].model, "claude-opus-fixture")
            self.assertEqual(data.history(chats[0])[-1].model, "claude-opus-fixture")

    def test_delivery_starts_resumable_cli_and_mentions_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            cwd = root / "project"
            cwd.mkdir()
            image = root / "upload.png"
            image.write_bytes(b"png")
            cli = root / "claude.exe"
            cli.write_bytes(b"fixture")
            chat = Chat("claude:session-id", "标题", str(cwd), "transcript.jsonl", 1, provider="claude")
            class Process:
                pid = 123
            with patch("claude_backend.subprocess.Popen", return_value=Process()) as launch:
                result = deliver_claude(str(cli), chat, "看图", [image], "request-id", run_dir=root / "runs",
                                        model="claude-opus-fixture")
            command = launch.call_args.args[0]
            self.assertIn("--resume", command)
            self.assertIn("session-id", command)
            self.assertIn("--add-dir", command)
            self.assertIn(str(image.parent), command)
            self.assertIn(str(image), command[-1])
            self.assertEqual(result["delivery_mode"], "claude-cli")
            self.assertEqual(result["process_id"], 123)
            self.assertEqual(command[command.index("--model") + 1], "claude-opus-fixture")
            self.assertNotIn("--fallback-model", command)
            self.assertEqual(result["requested_model"], "claude-opus-fixture")

    def test_missing_or_invalid_model_does_not_launch_cli(self):
        chat = Chat("claude:session-id", "标题", ".", "", 1, provider="claude")
        for model in ["", "--help", "opus\nhaiku", None]:
            with self.subTest(model=model), patch("claude_backend.subprocess.Popen") as launch:
                with self.assertRaises(ValueError):
                    deliver_claude("fixture", chat, "任务", [], "request-id", model=model)
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
