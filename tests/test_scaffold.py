"""Tests for the repository and integration scaffold."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "ezviz_iot_cn"


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_key_shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_key_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_key_shape(item) for item in value]
    return None


def test_repository_metadata_matches_project_identity() -> None:
    """The repository metadata must describe one CN custom integration."""
    manifest = _load_json(INTEGRATION / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")

    assert manifest == {
        "domain": "ezviz_iot_cn",
        "name": "EZVIZ IoT CN",
        "codeowners": ["@wxyjay"],
        "config_flow": True,
        "documentation": "https://github.com/wxyjay/ha-ezviz-iot-cn",
        "dependencies": ["webhook"],
        "integration_type": "hub",
        "iot_class": "cloud_polling",
        "issue_tracker": "https://github.com/wxyjay/ha-ezviz-iot-cn/issues",
        "requirements": [],
        "version": "0.1.0",
    }
    assert hacs == {
        "name": "EZVIZ IoT CN",
        "country": ["CN"],
        "homeassistant": "2026.9.0",
        "render_readme": True,
    }

    packaging = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert packaging["build-system"]["build-backend"] == "setuptools.build_meta"
    assert packaging["tool"]["setuptools"]["packages"]["find"] == {
        "include": ["custom_components*"],
        "namespaces": True,
    }
    assert packaging["tool"]["setuptools"]["package-data"][
        "custom_components.ezviz_iot_cn"
    ] == [
        "brand/*.png",
        "icons.json",
        "manifest.json",
        "strings.json",
        "translations/*.json",
    ]


def test_entity_icons_cover_user_visible_controls_and_events() -> None:
    """Custom icons distinguish controls and event semantics at a glance."""
    path = INTEGRATION / "icons.json"
    assert path.is_file()
    icons = _load_json(path)["entity"]

    assert icons["binary_sensor"]["person"]["state"] == {
        "off": "mdi:account-off",
        "on": "mdi:account",
    }
    assert icons["binary_sensor"]["doorbell_ring"]["state"]["on"] == (
        "mdi:bell-ring-outline"
    )
    assert icons["switch"]["defence"]["state"] == {
        "off": "mdi:shield-off",
        "on": "mdi:shield-check",
    }
    assert icons["select"]["alarm_sound_mode"]["state"] == {
        "short_alert": "mdi:bell",
        "long_alert": "mdi:bell-ring",
        "silent": "mdi:bell-off",
    }
    assert icons["sensor"]["calling_action"]["state"] == {
        "ring": "mdi:bell-ring",
        "answer": "mdi:phone-in-talk",
        "hang_up": "mdi:phone-hangup",
    }
    assert icons["binary_sensor"]["person_channel"] == icons["binary_sensor"]["person"]
    assert icons["button"]["ptz_up_channel"] == icons["button"]["ptz_up"]
    assert icons["image"]["latest_capture_channel"] == icons["image"]["latest_capture"]
    assert (
        icons["sensor"]["calling_action_channel"] == icons["sensor"]["calling_action"]
    )


def test_repository_contains_only_one_custom_integration() -> None:
    """HACS repositories must contain exactly one integration directory."""
    directories = sorted(
        path.name for path in (ROOT / "custom_components").iterdir() if path.is_dir()
    )

    assert directories == ["ezviz_iot_cn"]


def test_constants_define_bounded_intervals_and_webhook_namespace() -> None:
    """Core constants must be inspectable without importing Home Assistant."""
    module = ast.parse((INTEGRATION / "const.py").read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            values[target.id] = ast.literal_eval(node.value)

    assert values["DOMAIN"] == "ezviz_iot_cn"
    assert values["DEFAULT_ALARM_INTERVAL"] == 10
    assert values["MIN_ALARM_INTERVAL"] == 10
    assert values["MAX_ALARM_INTERVAL"] == 3600
    assert values["DEFAULT_STATE_INTERVAL"] == 60
    assert values["MIN_STATE_INTERVAL"] == 30
    assert values["MAX_STATE_INTERVAL"] == 3600
    assert values["DEFAULT_CAPTURE_INTERVAL"] == 600
    assert values["MIN_CAPTURE_INTERVAL"] == 300
    assert values["MAX_CAPTURE_INTERVAL"] == 86400
    assert values["WEBHOOK_ID_PREFIX"] == "ezviz_iot_cn_"


def test_strings_and_translation_have_matching_top_level_sections() -> None:
    """English source strings and Simplified Chinese translation ship together."""
    strings = _load_json(INTEGRATION / "strings.json")
    english = _load_json(INTEGRATION / "translations" / "en.json")
    chinese = _load_json(INTEGRATION / "translations" / "zh-Hans.json")

    assert strings
    assert english
    assert chinese
    assert strings == english
    assert set(strings) == set(chinese)
    assert _json_key_shape(strings) == _json_key_shape(chinese)
    assert {"config", "entity"}.issubset(strings)


def test_ui_strings_cover_event_entities_push_security_and_quota_guidance() -> None:
    """Both shipped languages describe Cloud Push value and its safety contract."""
    strings = _load_json(INTEGRATION / "strings.json")
    chinese = _load_json(INTEGRATION / "translations" / "zh-Hans.json")

    for document in (strings, chinese):
        entity = document["entity"]
        assert "doorbell_ring" in entity["binary_sensor"]
        assert "latest_calling" in entity["image"]
        assert {"last_calling_time", "calling_action"}.issubset(entity["sensor"])
        assert "devices" in document["config"]["step"]
        assert "device_filter_mode" in document["selector"]
        assert set(document["selector"]["device_filter_mode"]["options"]) == {
            "include",
            "exclude",
        }

        description = document["options"]["step"]["init"]["description"]
        for required in (
            "HTTPS",
            "HTTP 200",
            "messageId",
            "{webhook_url}",
            "{included_device_count}",
            "{estimated_daily_api_calls}",
            "{estimated_daily_image_downloads}",
            "https://open.ys7.com/help/19",
            "https://open.ys7.com/help/5130",
            "https://open.ys7.com/console/messagePush.html",
        ):
            assert required in description

    english_description = strings["options"]["step"]["init"]["description"]
    for required in (
        "near-real-time",
        "private webhook",
        "exact",
        "two seconds",
        "degrade",
        "missed",
        "estimate",
    ):
        assert required in english_description.lower()
    for forbidden in ("low-trust", "forge", "signature"):
        assert forbidden not in english_description.lower()

    chinese_description = chinese["options"]["step"]["init"]["description"]
    for required in (
        "接近实时",
        "私有 Webhook",
        "精确",
        "2 秒",
        "降级",
        "遗漏",
        "估算",
    ):
        assert required in chinese_description
    for forbidden in ("低信任", "伪造", "签名"):
        assert forbidden not in chinese_description


def test_public_repository_basics_exist() -> None:
    """The initial public repository documentation and validation files exist."""
    required = (
        ROOT / "LICENSE",
        ROOT / "CHANGELOG.md",
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / ".github" / "workflows" / "tests.yml",
        ROOT / ".github" / "workflows" / "validate.yml",
        INTEGRATION / "__init__.py",
    )

    assert all(path.is_file() for path in required)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "unofficial" in readme.lower()
    assert "China Open Platform" in readme
    assert "AppKey" in readme
    assert "AppSecret" in readme


def test_task_names_and_logging_do_not_reference_sensitive_runtime_values() -> None:
    """Observable internal labels must not interpolate private identifiers."""
    logging_methods = {"critical", "debug", "error", "exception", "info", "warning"}
    sensitive_names = {
        "_app_key",
        "_app_secret",
        "_device_serial",
        "access_token",
        "app_key",
        "app_secret",
        "device_serial",
        "image_url",
        "message_id",
        "webhook_id",
    }

    for path in INTEGRATION.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr == "create_task":
                task_name = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "name"
                    ),
                    None,
                )
                if task_name is not None:
                    referenced = {
                        child.id
                        for child in ast.walk(task_name)
                        if isinstance(child, ast.Name)
                    } | {
                        child.attr
                        for child in ast.walk(task_name)
                        if isinstance(child, ast.Attribute)
                    }
                    assert referenced.isdisjoint(sensitive_names), path
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOGGER"
                and node.func.attr in logging_methods
            ):
                referenced = {
                    child.id for child in ast.walk(node) if isinstance(child, ast.Name)
                } | {
                    child.attr
                    for child in ast.walk(node)
                    if isinstance(child, ast.Attribute)
                }
                assert referenced.isdisjoint(sensitive_names), path


def test_public_documentation_and_brand_are_release_ready() -> None:
    """Public docs and an original square icon ship with the integration."""
    api_mapping_path = ROOT / "docs" / "api-mapping.md"
    troubleshooting_path = ROOT / "docs" / "troubleshooting.md"
    icon_path = ROOT / "brand" / "icon.png"
    integration_icon_path = INTEGRATION / "brand" / "icon.png"
    integration_icon_2x_path = INTEGRATION / "brand" / "icon@2x.png"

    assert api_mapping_path.is_file()
    assert troubleshooting_path.is_file()
    assert icon_path.is_file()
    assert integration_icon_path.is_file()
    assert integration_icon_2x_path.is_file()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese_readme = (ROOT / "README_zh-Hans.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    normalized_chinese_readme = " ".join(chinese_readme.split())
    english_headings = (
        "## Integration scope",
        "## Core features",
        "## Installation",
        "## Add a hub and filter devices",
        "## Home Assistant entities",
        "## Cloud Push Webhook",
        "## App notification impact and usage guidance",
        "## API quota and polling",
        "## Security and privacy",
        "## Known limitations and troubleshooting",
        "## Removal",
        "## License",
    )
    chinese_headings = (
        "## 接入范围",
        "## 核心功能",
        "## 安装",
        "## 添加 Hub 与筛选设备",
        "## Home Assistant 实体",
        "## Cloud Push Webhook",
        "## App 通知影响与使用建议",
        "## API 配额与轮询",
        "## 安全与隐私",
        "## 已知限制与故障排查",
        "## 移除",
        "## 许可证",
    )
    assert [readme.index(heading) for heading in english_headings] == sorted(
        readme.index(heading) for heading in english_headings
    )
    assert [chinese_readme.index(heading) for heading in chinese_headings] == sorted(
        chinese_readme.index(heading) for heading in chinese_headings
    )

    for document in (readme, chinese_readme):
        for required in (
            "Cloud Push",
            "messageId",
            "/api/webhook/...",
            "https://open.ys7.com/help/5129",
            "AppKey",
            "AppSecret",
        ):
            assert required in document
        for forbidden in (
            "Scrypted",
            "low-trust",
            "低信任",
            "near-real-time",
            "接近实时",
            "after the first release",
            "正式发布后",
            "尚未发布正式版本",
        ):
            assert forbidden not in document

    for required in (
        "[Cloud Push Webhook](#cloud-push-webhook)",
        "without waiting for the next alarm poll",
        "sends the event directly to Home Assistant through the webhook",
        "[See usage guidance](#app-notification-solution)",
        "Message Test",
        "Actual device messages stop",
        "does not create a `camera` entity",
        "Use the primary account's Open Platform `AppKey` and `AppSecret` to "
        "add the Home Assistant hub",
    ):
        assert required in normalized_readme
    for required in (
        "[Cloud Push Webhook](#cloud-push-webhook)",
        "无需等待下一轮告警轮询",
        "通过 Webhook 将事件直接发送到 Home Assistant",
        "[查看使用建议](#app-notification-solution)",
        "消息测试",
        "实际设备消息将停止推送",
        "不会创建用于播放视频的 `camera` 实体",
        "使用主账号开放平台应用的 `AppKey` 和 `AppSecret` 添加 Home Assistant Hub",
    ):
        assert required in normalized_chinese_readme
    assert "Use the dedicated account's `AppKey`" not in normalized_readme
    assert "使用专用账号的 `AppKey`" not in normalized_chinese_readme
    assert "status=1" in readme
    assert "status=1" in chinese_readme
    assert "action=0" not in readme
    assert "action=0" not in chinese_readme

    api_mapping = api_mapping_path.read_text(encoding="utf-8")
    for endpoint in (
        "/token/get",
        "/device/list",
        "/device/status/get",
        "/camera/list",
        "/device/capacity",
        "/alarm/list",
        "/device/capture",
        "/device/defence/set",
        "/device/alarm/sound/set",
        "/device/ptz/start",
        "/device/ptz/stop",
    ):
        assert endpoint in api_mapping
    assert "Private mobile-app APIs" in api_mapping
    for reviewed_project in (
        "dscao/ezviz",
        "ha-china/ezviz_push",
        "liuzexier/ezviz_cn",
    ):
        assert reviewed_project in api_mapping
    assert "corroboration, not specification" in api_mapping.lower()
    assert "08905b0ed8a91a74e59c96355d154ad1f6ea1a31" in api_mapping
    assert "e60e27d5f2c46bd932685ad5f20ba0734ebeec61" in api_mapping
    assert "adbda754965bd5eeba0f5b77de35674318089943" in api_mapping
    assert "connection-time" in api_mapping
    assert "60-second overlap" in api_mapping
    assert "https://open.ys7.com/help/5130" in api_mapping
    assert "status` values 1/2/3" in api_mapping
    assert "actions 0/1/2" not in api_mapping

    privacy_section = readme.split("## Security and privacy", 1)[1]
    assert "Home Assistant's shared `aiohttp` session" in privacy_section
    assert "separate session" in privacy_section
    assert "connection-time" in privacy_section
    assert "every alarm-list request covers the latest 24 hours" not in readme

    troubleshooting = troubleshooting_path.read_text(encoding="utf-8")
    assert "Invalid authentication" in troubleshooting
    assert "API quota" in troubleshooting
    assert "Webhook" in troubleshooting
    assert "Scrypted" not in troubleshooting
    assert "low-trust" not in troubleshooting.lower()

    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "README_zh-Hans.md",
            troubleshooting_path,
            INTEGRATION / "strings.json",
            INTEGRATION / "translations" / "en.json",
            INTEGRATION / "translations" / "zh-Hans.json",
        )
    )
    assert "Scrypted" not in public_text
    assert "low-trust" not in public_text.lower()
    assert "低信任" not in public_text

    for path, expected_size in (
        (icon_path, 256),
        (integration_icon_path, 256),
        (integration_icon_2x_path, 512),
    ):
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == (expected_size, expected_size)

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" in changelog
    assert "EZVIZ China Open Platform" in changelog


def test_ci_workflows_cover_release_validation() -> None:
    """CI uses maintained actions and runs every local quality gate."""
    tests_workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    validate_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/checkout@v4" in tests_workflow
    assert "persist-credentials: false" in tests_workflow
    assert "python -m pytest -q" in tests_workflow
    assert "python -m ruff format --check ." in tests_workflow
    assert "python -m ruff check ." in tests_workflow
    assert "python -m mypy custom_components/ezviz_iot_cn" in tests_workflow
    assert "python -m compileall -q custom_components" in tests_workflow
    assert "Check localization parity" in tests_workflow

    assert "hacs/action@22.5.0" in validate_workflow
    assert "actions/checkout@v4" in validate_workflow
    assert "persist-credentials: false" in validate_workflow
    assert "home-assistant/actions/hassfest@master" in validate_workflow
