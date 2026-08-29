import unittest

from app import validate


class ValidateChatRequestTests(unittest.TestCase):
    def test_accepts_minimal_chat(self):
        result = validate(
            {"model": "qwen3:8b", "messages": [{"role": "user", "content": "Hi"}]}
        )
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertEqual(result["messages"][0]["content"], "Hi")

    def test_drops_untrusted_message_fields(self):
        result = validate(
            {
                "model": "test",
                "messages": [{"role": "user", "content": "Hi", "images": ["ignored"]}],
                "stream": False,
            }
        )
        self.assertEqual(result, {"model": "test", "messages": [{"role": "user", "content": "Hi"}]})

    def test_rejects_bad_role(self):
        with self.assertRaisesRegex(ValueError, "invalid role"):
            validate(
                {"model": "test", "messages": [{"role": "root", "content": "Hi"}]}
            )

    def test_rejects_empty_messages(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            validate({"model": "test", "messages": []})


if __name__ == "__main__":
    unittest.main()
