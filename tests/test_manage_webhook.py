import unittest
from unittest.mock import patch

from scripts import manage_webhook


class CommandMenuTests(unittest.TestCase):
    @patch.object(manage_webhook, "api_post")
    def test_user_and_admin_command_scopes_are_separate(self, api_post) -> None:
        manage_webhook.configure_command_menus("https://telegram.test", {20, 10})

        self.assertEqual(api_post.call_count, 3)
        user_payload = api_post.call_args_list[0].args[2]
        first_admin_payload = api_post.call_args_list[1].args[2]
        second_admin_payload = api_post.call_args_list[2].args[2]

        self.assertEqual(user_payload["scope"], {"type": "all_private_chats"})
        self.assertEqual(user_payload["commands"], manage_webhook.USER_COMMANDS)
        self.assertEqual(
            first_admin_payload["scope"], {"type": "chat", "chat_id": 10}
        )
        self.assertEqual(
            second_admin_payload["scope"], {"type": "chat", "chat_id": 20}
        )
        self.assertEqual(first_admin_payload["commands"], manage_webhook.ADMIN_COMMANDS)

    def test_command_definitions_are_valid_and_unique(self) -> None:
        for commands in (
            manage_webhook.USER_COMMANDS,
            manage_webhook.ADMIN_COMMANDS,
        ):
            names = [item["command"] for item in commands]
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(name.replace("_", "").isalnum() for name in names))
            self.assertTrue(all(item["description"] for item in commands))


if __name__ == "__main__":
    unittest.main()
