#!/usr/bin/env python3
"""Monitor App Store prices and versions and notify through Telegram."""

from __future__ import annotations

import argparse
import copy
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("app_store_monitor")
CACHE_SCHEMA_VERSION = 1
DEFAULT_USER_AGENT = "app-store-monitor/1.0"


class MonitorError(Exception):
    """Base exception for expected monitor errors."""


class ConfigError(MonitorError):
    """Raised when configuration or cache data is invalid."""


class ResponseValidationError(MonitorError):
    """Raised when a remote service returns an invalid payload."""


class RequestFailed(MonitorError):
    """Raised after all request attempts fail."""


@dataclass(frozen=True)
class AppConfig:
    app_id: str
    countries: tuple[str, ...]
    watch_price: bool
    watch_version: bool


@dataclass(frozen=True)
class Settings:
    retries: int
    timeout_seconds: float
    bot_token: str
    chat_id: str
    apps: tuple[AppConfig, ...]


@dataclass(frozen=True)
class Observation:
    name: str
    url: str
    price: Decimal | None = None
    currency: str | None = None
    version: str | None = None


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} 必须是 JSON 对象")
    return value


def _parse_countries(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_countries = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw_countries = value
    else:
        raise ConfigError(f"{label} 必须是逗号分隔的字符串或字符串数组")

    countries: list[str] = []
    for raw_country in raw_countries:
        country = raw_country.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ConfigError(f"{label} 包含无效地区代码：{raw_country!r}")
        if country not in countries:
            countries.append(country)
    if not countries:
        raise ConfigError(f"{label} 不能为空")
    return tuple(countries)


def load_settings(path: Path) -> Settings:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            root = json.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    root = _require_mapping(root, "配置根节点")
    request_config = _require_mapping(root.get("request", {}), "request")
    telegram_config = _require_mapping(root.get("telegram", {}), "telegram")

    retries = request_config.get("retries", 3)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ConfigError("request.retries 必须是大于或等于 0 的整数")

    timeout = request_config.get("timeout_seconds", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("request.timeout_seconds 必须是大于 0 的数字")

    # Environment variables deliberately take precedence over values in the file.
    bot_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN", str(telegram_config.get("bot_token", ""))
    ).strip()
    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID", str(telegram_config.get("chat_id", ""))
    ).strip()

    raw_apps = root.get("apps")
    if not isinstance(raw_apps, list) or not raw_apps:
        raise ConfigError("apps 必须是非空数组")

    apps: list[AppConfig] = []
    seen_ids: set[str] = set()
    for index, raw_app in enumerate(raw_apps):
        label = f"apps[{index}]"
        raw_app = _require_mapping(raw_app, label)
        app_id = str(raw_app.get("app_id", "")).strip()
        if not re.fullmatch(r"[1-9][0-9]*", app_id):
            raise ConfigError(f"{label}.app_id 必须是正整数形式的 App Store ID")
        if app_id in seen_ids:
            raise ConfigError(f"配置中存在重复的应用 ID：{app_id}")
        seen_ids.add(app_id)

        countries = _parse_countries(raw_app.get("countries"), f"{label}.countries")
        watch_price = raw_app.get("watch_price", False)
        watch_version = raw_app.get("watch_version", False)
        if not isinstance(watch_price, bool) or not isinstance(watch_version, bool):
            raise ConfigError(
                f"{label}.watch_price 和 {label}.watch_version 必须是布尔值"
            )
        if not watch_price and not watch_version:
            raise ConfigError(f"{label} 至少需要启用一种监听")

        apps.append(
            AppConfig(
                app_id=app_id,
                countries=countries,
                watch_price=watch_price,
                watch_version=watch_version,
            )
        )

    return Settings(
        retries=retries,
        timeout_seconds=float(timeout),
        bot_token=bot_token,
        chat_id=chat_id,
        apps=tuple(apps),
    )


def _request_json(
    request: Request,
    *,
    timeout: float,
    retries: int,
    label: str,
    validator: Callable[[Any], Any],
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    """Request JSON, validate it, and retry failures with bounded backoff."""
    last_error = "未知错误"
    total_attempts = retries + 1
    for attempt in range(total_attempts):
        try:
            with opener(request, timeout=timeout) as response:
                status = response.getcode()
                if status != 200:
                    raise ResponseValidationError(f"HTTP 状态码为 {status}")
                payload = json.loads(response.read().decode("utf-8"))
                return validator(payload)
        except HTTPError as exc:
            last_error = f"HTTP 状态码为 {exc.code}"
        except URLError as exc:
            last_error = f"网络错误：{exc.reason}"
        except TimeoutError:
            last_error = "请求超时"
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = f"响应不是有效 JSON：{exc}"
        except ResponseValidationError as exc:
            last_error = str(exc)
        except OSError as exc:
            last_error = f"网络错误：{exc}"

        if attempt < retries:
            delay = min(2**attempt, 8)
            LOGGER.warning(
                "%s失败（第 %d/%d 次）：%s；%s 秒后重试",
                label,
                attempt + 1,
                total_attempts,
                last_error,
                delay,
            )
            sleeper(delay)

    raise RequestFailed(f"{label}在 {total_attempts} 次尝试后失败：{last_error}")


def _decimal_from_value(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ResponseValidationError(f"{label} 不是有效价格")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ResponseValidationError(f"{label} 不是有效价格") from exc
    if not result.is_finite() or result < 0:
        raise ResponseValidationError(f"{label} 不是有效价格")
    return result


def _validate_lookup_payload(
    payload: Any, *, need_price: bool, need_version: bool
) -> Observation:
    if not isinstance(payload, dict) or payload.get("resultCount") != 1:
        raise ResponseValidationError("resultCount 不等于 1")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ResponseValidationError("results 不是包含一个应用的数组")

    result = results[0]
    name = result.get("trackCensoredName")
    app_url = result.get("trackViewUrl")
    if not isinstance(name, str) or not name.strip():
        raise ResponseValidationError("缺少 trackCensoredName")
    if not isinstance(app_url, str) or not app_url.strip():
        raise ResponseValidationError("缺少 trackViewUrl")

    price: Decimal | None = None
    currency: str | None = None
    version: str | None = None
    if need_price:
        if "price" not in result:
            raise ResponseValidationError("缺少 price")
        price = _decimal_from_value(result["price"], "price")
        raw_currency = result.get("currency")
        if not isinstance(raw_currency, str) or not raw_currency.strip():
            raise ResponseValidationError("缺少 currency")
        currency = raw_currency.strip().upper()
    if need_version:
        raw_version = result.get("version")
        if not isinstance(raw_version, str) or not raw_version.strip():
            raise ResponseValidationError("缺少 version")
        version = raw_version.strip()

    return Observation(
        name=name.strip(),
        url=app_url.strip(),
        price=price,
        currency=currency,
        version=version,
    )


class AppStoreClient:
    def __init__(self, timeout: float, retries: int) -> None:
        self.timeout = timeout
        self.retries = retries

    def lookup(
        self,
        app_id: str,
        country: str,
        *,
        need_price: bool,
        need_version: bool,
    ) -> Observation:
        query = urlencode({"id": app_id, "country": country})
        request = Request(
            f"https://itunes.apple.com/lookup?{query}",
            headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT},
            method="GET",
        )
        return _request_json(
            request,
            timeout=self.timeout,
            retries=self.retries,
            label=f"查询应用 {app_id}（{country}）",
            validator=lambda payload: _validate_lookup_payload(
                payload, need_price=need_price, need_version=need_version
            ),
        )


def _canonical_price(value: Decimal) -> str:
    raw = format(value, "f")
    whole, separator, fraction = raw.partition(".")
    if not separator:
        fraction = ""
    fraction = fraction.rstrip("0")
    fraction = fraction + "0" * max(0, 2 - len(fraction))
    return f"{whole}.{fraction}"


def _format_price(price: str, currency: str) -> str:
    return f"{currency} {price}"


def _price_message(
    observation: Observation,
    country: str,
    old_price: str,
    old_currency: str,
    new_price: str,
    new_currency: str,
) -> str:
    return "\n".join(
        (
            "🔔 <b>App Store 价格变化</b>",
            f"应用：<a href=\"{html.escape(observation.url, quote=True)}\">"
            f"{html.escape(observation.name)}</a>",
            f"地区：<code>{html.escape(country)}</code>",
            "变化："
            f"<code>{html.escape(_format_price(old_price, old_currency))}</code>"
            " → "
            f"<code>{html.escape(_format_price(new_price, new_currency))}</code>",
        )
    )


def _version_message(
    observation: Observation, country: str, old_version: str, new_version: str
) -> str:
    return "\n".join(
        (
            "🔔 <b>App Store 版本变化</b>",
            f"应用：<a href=\"{html.escape(observation.url, quote=True)}\">"
            f"{html.escape(observation.name)}</a>",
            f"取值地区：<code>{html.escape(country)}</code>",
            f"变化：<code>{html.escape(old_version)}</code> → "
            f"<code>{html.escape(new_version)}</code>",
        )
    )


class TelegramClient:
    def __init__(
        self, bot_token: str, chat_id: str, timeout: float, retries: int
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.retries = retries

    @staticmethod
    def _validate_response(payload: Any) -> Any:
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            description = "未知错误"
            if isinstance(payload, dict) and isinstance(payload.get("description"), str):
                description = payload["description"]
            raise ResponseValidationError(f"Telegram 返回失败：{description}")
        return payload

    def send(self, message: str) -> None:
        if not self.bot_token or not self.chat_id:
            raise RequestFailed(
                "检测到变化，但未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"
            )
        body = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        _request_json(
            request,
            timeout=self.timeout,
            retries=self.retries,
            label="发送 Telegram 通知",
            validator=self._validate_response,
        )


def _validate_cache(root: Any, path: Path) -> dict[str, Any]:
    if not isinstance(root, dict):
        raise ConfigError(f"缓存文件 {path} 的根节点不是 JSON 对象")
    if root.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ConfigError(
            f"缓存文件 {path} 的 schema_version 不受支持（应为 {CACHE_SCHEMA_VERSION}）"
        )
    apps = root.get("apps")
    if not isinstance(apps, dict):
        raise ConfigError(f"缓存文件 {path} 的 apps 不是 JSON 对象")

    for app_id, app_state in apps.items():
        if not isinstance(app_id, str) or not isinstance(app_state, dict):
            raise ConfigError(f"缓存文件 {path} 包含无效的应用记录")
        if "version" in app_state and not isinstance(app_state["version"], str):
            raise ConfigError(f"缓存中应用 {app_id} 的 version 无效")
        prices = app_state.get("prices", {})
        if not isinstance(prices, dict):
            raise ConfigError(f"缓存中应用 {app_id} 的 prices 无效")
        for country, price_state in prices.items():
            if not isinstance(country, str) or not isinstance(price_state, dict):
                raise ConfigError(f"缓存中应用 {app_id} 包含无效的价格记录")
            if "price" not in price_state:
                raise ConfigError(f"缓存中应用 {app_id}/{country} 缺少 price")
            try:
                _decimal_from_value(price_state["price"], "缓存价格")
            except ResponseValidationError as exc:
                raise ConfigError(f"缓存中应用 {app_id}/{country} 的 price 无效") from exc
            if not isinstance(price_state.get("currency"), str) or not price_state[
                "currency"
            ].strip():
                raise ConfigError(f"缓存中应用 {app_id}/{country} 的 currency 无效")
    return root


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": CACHE_SCHEMA_VERSION, "apps": {}}
    try:
        with path.open("r", encoding="utf-8") as cache_file:
            return _validate_cache(json.load(cache_file), path)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"缓存文件 {path} 不是有效 JSON：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取缓存文件 {path}：{exc}") from exc


def _scoped_cache(old_cache: dict[str, Any], apps: tuple[AppConfig, ...]) -> dict[str, Any]:
    """Keep only data that is still part of the current monitoring scope."""
    result: dict[str, Any] = {"schema_version": CACHE_SCHEMA_VERSION, "apps": {}}
    old_apps = old_cache["apps"]
    for app in apps:
        old_state = old_apps.get(app.app_id)
        if not isinstance(old_state, dict):
            continue
        state: dict[str, Any] = {}
        if app.watch_version and isinstance(old_state.get("version"), str):
            state["version"] = old_state["version"]
        if app.watch_price:
            old_prices = old_state.get("prices", {})
            prices = {
                country: copy.deepcopy(old_prices[country])
                for country in app.countries
                if country in old_prices
            }
            if prices:
                state["prices"] = prices
        if state:
            result["apps"][app.app_id] = state
    return result


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary_file:
            temporary_name = temporary_file.name
            json.dump(cache, temporary_file, ensure_ascii=False, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise ConfigError(f"无法写入缓存文件 {path}：{exc}") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def run_monitor(
    settings: Settings,
    cache_path: Path,
    *,
    app_store: Any | None = None,
    telegram: Any | None = None,
) -> bool:
    """Run one monitoring pass. Return True when every operation succeeds."""
    old_cache = load_cache(cache_path)
    cache = _scoped_cache(old_cache, settings.apps)
    app_store = app_store or AppStoreClient(
        settings.timeout_seconds, settings.retries
    )
    telegram = telegram or TelegramClient(
        settings.bot_token,
        settings.chat_id,
        settings.timeout_seconds,
        settings.retries,
    )
    all_succeeded = True

    for app in settings.apps:
        observations: dict[str, Observation] = {}
        countries_to_query = app.countries if app.watch_price else app.countries[:1]
        for country in countries_to_query:
            try:
                observations[country] = app_store.lookup(
                    app.app_id,
                    country,
                    need_price=app.watch_price,
                    need_version=app.watch_version and country == app.countries[0],
                )
                LOGGER.info("已查询应用 %s（%s）", app.app_id, country)
            except RequestFailed as exc:
                all_succeeded = False
                LOGGER.error("%s", exc)

        app_state = cache["apps"].setdefault(app.app_id, {})
        first_country = app.countries[0]

        if app.watch_version and first_country in observations:
            observation = observations[first_country]
            assert observation.version is not None
            old_version = app_state.get("version")
            if old_version is None:
                app_state["version"] = observation.version
                LOGGER.info("首次缓存应用 %s 的版本 %s", app.app_id, observation.version)
            elif old_version != observation.version:
                try:
                    telegram.send(
                        _version_message(
                            observation,
                            first_country,
                            old_version,
                            observation.version,
                        )
                    )
                    app_state["version"] = observation.version
                    LOGGER.info(
                        "已通知应用 %s 的版本变化：%s -> %s",
                        app.app_id,
                        old_version,
                        observation.version,
                    )
                except RequestFailed as exc:
                    all_succeeded = False
                    LOGGER.error("应用 %s 的版本通知失败：%s", app.app_id, exc)

        if app.watch_price:
            prices = app_state.setdefault("prices", {})
            for country in app.countries:
                observation = observations.get(country)
                if observation is None:
                    continue
                assert observation.price is not None and observation.currency is not None
                new_price = _canonical_price(observation.price)
                new_currency = observation.currency
                old_price_state = prices.get(country)
                if old_price_state is None:
                    prices[country] = {"price": new_price, "currency": new_currency}
                    LOGGER.info(
                        "首次缓存应用 %s（%s）的价格 %s",
                        app.app_id,
                        country,
                        _format_price(new_price, new_currency),
                    )
                    continue

                old_price = _canonical_price(
                    _decimal_from_value(old_price_state["price"], "缓存价格")
                )
                old_currency = old_price_state["currency"].strip().upper()
                if old_price != new_price or old_currency != new_currency:
                    try:
                        telegram.send(
                            _price_message(
                                observation,
                                country,
                                old_price,
                                old_currency,
                                new_price,
                                new_currency,
                            )
                        )
                        prices[country] = {
                            "price": new_price,
                            "currency": new_currency,
                        }
                        LOGGER.info(
                            "已通知应用 %s（%s）的价格变化：%s -> %s",
                            app.app_id,
                            country,
                            _format_price(old_price, old_currency),
                            _format_price(new_price, new_currency),
                        )
                    except RequestFailed as exc:
                        all_succeeded = False
                        LOGGER.error(
                            "应用 %s（%s）的价格通知失败：%s",
                            app.app_id,
                            country,
                            exc,
                        )
                else:
                    # Normalize legacy numeric cache values without creating a change.
                    prices[country] = {
                        "price": new_price,
                        "currency": new_currency,
                    }

        if not app_state:
            del cache["apps"][app.app_id]

    save_cache(cache_path, cache)
    return all_succeeded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="查询 App Store 应用价格和版本，并在变化时发送 Telegram 通知。"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.json"), help="配置文件路径"
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("cache.json"), help="缓存文件路径"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    try:
        settings = load_settings(args.config)
        succeeded = run_monitor(settings, args.cache)
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.error("运行已取消")
        return 130
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
