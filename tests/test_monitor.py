import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app_store_monitor import (
    AppConfig,
    ConfigError,
    Observation,
    RequestFailed,
    ResponseValidationError,
    Settings,
    _request_json,
    load_cache,
    load_settings,
    run_monitor,
)


def make_settings(*apps: AppConfig) -> Settings:
    return Settings(
        retries=3,
        timeout_seconds=5,
        bot_token="",
        chat_id="",
        apps=apps,
    )


class FakeAppStore:
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def lookup(self, app_id, country, *, need_price, need_version):
        self.calls.append((app_id, country, need_price, need_version))
        result = self.observations[(app_id, country)]
        if isinstance(result, Exception):
            raise result
        return result


class FakeTelegram:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, message):
        self.messages.append(message)
        if self.fail:
            raise RequestFailed("telegram failed")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self.payload


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary_directory.name) / "cache.json"
        self.app = AppConfig("123", ("US", "CN"), True, True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def observation(country, price, version=None):
        return Observation(
            name=f"Example {country}",
            url=f"https://apps.apple.com/{country.lower()}/app/id123",
            price=Decimal(price),
            currency="USD" if country == "US" else "CNY",
            version=version,
        )

    def test_first_run_only_populates_cache(self):
        store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "0", "1.0"),
                ("123", "CN"): self.observation("CN", "12.5"),
            }
        )
        telegram = FakeTelegram()

        self.assertTrue(
            run_monitor(
                make_settings(self.app),
                self.cache_path,
                app_store=store,
                telegram=telegram,
            )
        )

        self.assertEqual([], telegram.messages)
        cache = load_cache(self.cache_path)
        self.assertEqual("1.0", cache["apps"]["123"]["version"])
        self.assertEqual(
            {"price": "0.00", "currency": "USD"},
            cache["apps"]["123"]["prices"]["US"],
        )
        self.assertEqual(
            {"price": "12.50", "currency": "CNY"},
            cache["apps"]["123"]["prices"]["CN"],
        )

    def test_version_and_each_country_price_are_separate_messages(self):
        initial_store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "1.99", "1.0"),
                ("123", "CN"): self.observation("CN", "12.00"),
            }
        )
        run_monitor(
            make_settings(self.app),
            self.cache_path,
            app_store=initial_store,
            telegram=FakeTelegram(),
        )

        changed_store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "0", "2.0"),
                ("123", "CN"): self.observation("CN", "6.00"),
            }
        )
        telegram = FakeTelegram()
        self.assertTrue(
            run_monitor(
                make_settings(self.app),
                self.cache_path,
                app_store=changed_store,
                telegram=telegram,
            )
        )

        self.assertEqual(3, len(telegram.messages))
        self.assertIn("版本变化", telegram.messages[0])
        self.assertIn("1.0", telegram.messages[0])
        self.assertIn("2.0", telegram.messages[0])
        self.assertIn("US", telegram.messages[1])
        self.assertIn("USD 1.99", telegram.messages[1])
        self.assertIn("USD 0.00", telegram.messages[1])
        self.assertIn("CN", telegram.messages[2])

    def test_new_country_is_cached_without_notification(self):
        first_app = AppConfig("123", ("US",), True, False)
        run_monitor(
            make_settings(first_app),
            self.cache_path,
            app_store=FakeAppStore(
                {("123", "US"): self.observation("US", "1.99")}
            ),
            telegram=FakeTelegram(),
        )

        expanded_app = AppConfig("123", ("US", "CN"), True, False)
        telegram = FakeTelegram()
        run_monitor(
            make_settings(expanded_app),
            self.cache_path,
            app_store=FakeAppStore(
                {
                    ("123", "US"): self.observation("US", "1.99"),
                    ("123", "CN"): self.observation("CN", "12.00"),
                }
            ),
            telegram=telegram,
        )
        self.assertEqual([], telegram.messages)
        self.assertIn("CN", load_cache(self.cache_path)["apps"]["123"]["prices"])

    def test_failed_notification_does_not_advance_cache(self):
        first_store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "1.99", "1.0"),
                ("123", "CN"): self.observation("CN", "12.00"),
            }
        )
        run_monitor(
            make_settings(self.app),
            self.cache_path,
            app_store=first_store,
            telegram=FakeTelegram(),
        )

        changed_store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "0", "2.0"),
                ("123", "CN"): self.observation("CN", "6.00"),
            }
        )
        self.assertFalse(
            run_monitor(
                make_settings(self.app),
                self.cache_path,
                app_store=changed_store,
                telegram=FakeTelegram(fail=True),
            )
        )
        cache = load_cache(self.cache_path)["apps"]["123"]
        self.assertEqual("1.0", cache["version"])
        self.assertEqual("1.99", cache["prices"]["US"]["price"])
        self.assertEqual("12.00", cache["prices"]["CN"]["price"])

    def test_failed_region_preserves_old_cache_and_returns_failure(self):
        initial = {
            "schema_version": 1,
            "apps": {
                "123": {
                    "version": "1.0",
                    "prices": {
                        "US": {"price": "1.99", "currency": "USD"},
                        "CN": {"price": "12.00", "currency": "CNY"},
                    },
                }
            },
        }
        self.cache_path.write_text(json.dumps(initial), encoding="utf-8")
        store = FakeAppStore(
            {
                ("123", "US"): self.observation("US", "1.99", "1.0"),
                ("123", "CN"): RequestFailed("lookup failed"),
            }
        )
        self.assertFalse(
            run_monitor(
                make_settings(self.app),
                self.cache_path,
                app_store=store,
                telegram=FakeTelegram(),
            )
        )
        self.assertEqual(
            "12.00", load_cache(self.cache_path)["apps"]["123"]["prices"]["CN"]["price"]
        )

    def test_version_only_queries_first_country(self):
        app = AppConfig("123", ("US", "CN"), False, True)
        store = FakeAppStore(
            {("123", "US"): self.observation("US", "0", "1.0")}
        )
        run_monitor(
            make_settings(app),
            self.cache_path,
            app_store=store,
            telegram=FakeTelegram(),
        )
        self.assertEqual([("123", "US", False, True)], store.calls)


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_file_and_countries_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "telegram": {"bot_token": "file-token", "chat_id": "file-chat"},
                        "apps": [
                            {
                                "app_id": 123,
                                "countries": "us, CN,us",
                                "watch_price": True,
                                "watch_version": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"TELEGRAM_BOT_TOKEN": "env-token", "TELEGRAM_CHAT_ID": "env-chat"},
            ):
                settings = load_settings(path)

        self.assertEqual("env-token", settings.bot_token)
        self.assertEqual("env-chat", settings.chat_id)
        self.assertEqual(("US", "CN"), settings.apps[0].countries)
        self.assertEqual(3, settings.retries)
        self.assertEqual(5, settings.timeout_seconds)

    def test_rejects_app_with_no_enabled_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {
                                "app_id": "123",
                                "countries": "US",
                                "watch_price": False,
                                "watch_version": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_settings(path)


class RequestTests(unittest.TestCase):
    def test_invalid_payload_is_retried(self):
        responses = iter(
            [
                FakeResponse(b'{"ok": false}'),
                FakeResponse(b'{"ok": true}'),
            ]
        )
        sleeps = []

        def opener(request, timeout):
            self.assertEqual(5, timeout)
            return next(responses)

        def validator(payload):
            if payload.get("ok") is not True:
                raise ResponseValidationError("not ok")
            return payload

        result = _request_json(
            object(),
            timeout=5,
            retries=1,
            label="test request",
            validator=validator,
            opener=opener,
            sleeper=sleeps.append,
        )

        self.assertEqual({"ok": True}, result)
        self.assertEqual([1], sleeps)


if __name__ == "__main__":
    unittest.main()
