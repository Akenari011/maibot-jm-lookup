import asyncio
import importlib.util
import logging
import random
import sys
import types
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple

import pytest

PLUGIN_PATH = Path(__file__).parents[1] / "plugin.py"
spec = importlib.util.spec_from_file_location("manga_plugin_test", PLUGIN_PATH)
plugin_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)
namespace = {**vars(plugin_module), "Any": Any, "ClassVar": ClassVar, "Literal": Literal, "NamedTuple": NamedTuple}
plugin_module.JmConfig.model_rebuild(force=True, _types_namespace=namespace)
plugin_module.RootConfig.model_rebuild(force=True, _types_namespace=namespace)

@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "TEST_ROOT", tmp_path)
    monkeypatch.setattr(plugin_module, "_http_get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network disabled in tests")))


def make_plugin():
    plugin = plugin_module.JmLookupPlugin()
    plugin._ctx = types.SimpleNamespace(
        logger=logging.getLogger("manga-test"),
        paths=types.SimpleNamespace(data_dir=TEST_ROOT, runtime_dir=TEST_ROOT),
    )
    plugin.set_plugin_config(plugin_module.RootConfig().model_dump())
    plugin._session_overrides = {}
    plugin._proxy = lambda: ""
    return plugin


def test_search_respects_per_group_adult_override(monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    plugin._session_overrides["123"] = {"adult": False}
    called = []
    def factory(prefix):
        class Provider:
            def __init__(self, *args, **kwargs):
                self.prefix = prefix
            async def search(self, *args):
                called.append(prefix)
                return [plugin_module.LookupResult(prefix, url=f"https://fixture/{prefix}")]
        return Provider
    for name, prefix in [("CopyMangaProvider", "copy"), ("GMangaProvider", "gm"), ("NhentaiProvider", "nh"), ("EHentaiProvider", "eh")]:
        monkeypatch.setattr(plugin_module, name, factory(prefix))
    results = asyncio.run(plugin._search_channels("query", 5, 1, session_key="qq_group_123"))
    assert called == ["copy", "gm"]
    assert [item.title for item in results] == ["copy", "gm"]


def test_string_booleans_in_session_override_are_not_truthy_strings():
    plugin = make_plugin()
    result = asyncio.run(plugin.api_webui_save_session(payload='{"session":"g1","adult":"false","send_cover":"false"}'))
    assert result["entry"] == {"send_cover": False, "adult": False}


def test_session_override_rejects_invalid_boolean():
    plugin = make_plugin()
    with pytest.raises(ValueError):
        asyncio.run(plugin.api_webui_save_session(payload='{"session":"g1","adult":"disabled"}'))


def test_nh_tag_search_keeps_sfw_filter(monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.adult_declared = True
    plugin.config.jm.adult_enabled = True
    plugin.config.jm.sfw_only = True
    seen = []
    class Provider:
        prefix = "nh"
        async def search(self, query, limit, timeout):
            seen.append(query)
            return []
    monkeypatch.setattr(plugin_module, "NhentaiProvider", lambda *a, **k: Provider())
    asyncio.run(plugin._search_channels("yuri", 5, 1, tag_mode=True, session_key="g1"))
    assert seen == ["tag:yuri tag:non-h"]


def test_copy_details_use_auto_probed_host(monkeypatch):
    provider = plugin_module.CopyMangaProvider()
    monkeypatch.setattr(provider, "_host", lambda timeout: asyncio.sleep(0, result="mirror.example"))
    seen = []
    def fake_get(url, timeout, headers=None, proxy=""):
        seen.append((url, headers or {}))
        return b'<h6 title="name"></h6>'
    monkeypatch.setattr(plugin_module, "_http_get", fake_get)
    asyncio.run(provider.detail("slug", 1))
    assert seen and "mirror.example" in seen[0][0]
    assert "mirror.example" in seen[0][1]["Referer"]


def test_copy_search_uses_probed_host_for_both_requests(monkeypatch):
    provider = plugin_module.CopyMangaProvider()
    monkeypatch.setattr(provider, "_host", lambda timeout: asyncio.sleep(0, result="mirror.example"))
    requests = []
    def fake_get(url, timeout, headers=None, proxy=""):
        requests.append((url, headers or {}))
        return b'const countApi = "/api/search"' if url.endswith("/search") else b'{"results":{"list":[]}}'
    monkeypatch.setattr(plugin_module, "_http_get", fake_get)
    asyncio.run(provider.search("title", 2, 1))
    assert len(requests) == 2
    assert all("mirror.example" in url and "mirror.example" in headers.get("Referer", "") for url, headers in requests)


def test_daily_count_builds_requested_number_of_distinct_items(monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.daily_count = 2
    monkeypatch.setattr(random, "choice", lambda values: values[0])
    class Provider:
        prefix = "copy"
        async def search(self, query, limit, timeout):
            return [plugin_module.LookupResult(str(i), url=f"https://site/{i}") for i in range(4)]
    plugin._daily_providers = lambda *args: [Provider()]
    plugin._llm_pick = lambda cands, kw: asyncio.sleep(0, result=0)
    result = asyncio.run(plugin._daily_build_items(2, "g1", False))
    assert len(result) == 2
    assert result[0][1].url != result[1][1].url


def test_daily_targets_build_candidates_per_target_and_gate():
    plugin = make_plugin()
    plugin.config.jm.daily_targets = ["qq_group_1", "qq_group_2"]
    plugin.config.jm.adult_declared = True
    plugin.config.jm.adult_enabled = True
    plugin._session_overrides["1"] = {"adult": False}
    seen = []
    async def build(count, key, private):
        seen.append((key, plugin._adult_on(key, private)))
        return []
    plugin._daily_build_items = build
    plugin._resolve_daily_targets = lambda: asyncio.sleep(0, result=plugin.config.jm.daily_targets)
    asyncio.run(plugin._daily_push())
    assert seen == [("qq_group_1", False), ("qq_group_2", True)]


def test_command_gate_honors_global_private_and_group_settings():
    plugin = make_plugin()
    plugin.config.jm.allow_private = False
    assert not plugin._command_allowed({"stream_id": "qq_private_99"})
    plugin.config.jm.allowed_groups = ["123"]
    assert plugin._command_allowed({"stream_id": "qq_group_123"})
    assert not plugin._command_allowed({"stream_id": "qq_group_456"})
    plugin.config.jm.enabled = False
    assert not plugin._command_allowed({"stream_id": "qq_group_123"})


def test_cover_override_applies_by_group_number():
    plugin = make_plugin()
    plugin.config.jm.send_cover = True
    plugin._session_overrides["123"] = {"send_cover": False}
    assert not plugin._effective_session_bool("send_cover", True, "qq_group_123", False)


def test_config_sections_writer_escapes_and_matches_section(tmp_path, monkeypatch):
    import tomllib
    plugin = make_plugin()
    config_path = tmp_path / "config.toml"
    config_path.write_text("[plugin]\nenabled = true\nconfig_version = \"1.0.0\"\n\n[jm]\nproxy_url = \"\"\n", encoding="utf-8")
    monkeypatch.setattr(plugin_module, "__file__", str(tmp_path / "plugin.py"))
    plugin._write_toml_sections({"jm": {"proxy_url": 'http://proxy/"quoted"', "daily_count": 3}})
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["plugin"]["enabled"] is True
    assert parsed["jm"]["proxy_url"] == 'http://proxy/"quoted"'
    assert parsed["jm"]["daily_count"] == 3


def test_config_write_escapes_strings_and_updates_correct_section(tmp_path, monkeypatch):
    import tomllib
    plugin = make_plugin()
    config_path = tmp_path / "config.toml"
    config_path.write_text("[plugin]\nenabled = true\nconfig_version = \"1.0.0\"\n\n[jm]\nproxy_url = \"\"\n", encoding="utf-8")
    monkeypatch.setattr(plugin_module, "__file__", str(tmp_path / "plugin.py"))
    plugin._write_config_values({"proxy_url": "http://proxy/\"quoted\"", "daily_count": 2})
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["plugin"]["enabled"] is True
    assert data["jm"]["proxy_url"] == 'http://proxy/"quoted"'
    assert data["jm"]["daily_count"] == 2


def test_daily_count_config_exists():
    assert plugin_module.JmConfig.model_fields["daily_count"].default == 1


def test_unload_cancels_and_awaits_background_tasks():
    plugin = make_plugin()
    async def run():
        task = asyncio.create_task(asyncio.sleep(3600))
        plugin._bg_tasks.add(task)
        plugin._daily_task = asyncio.create_task(asyncio.sleep(3600))
        await plugin.on_unload()
        assert task.cancelled()
        assert plugin._daily_task is None
        assert not plugin._bg_tasks
    asyncio.run(run())


def test_invalid_config_keeps_original_file(tmp_path, monkeypatch):
    plugin = make_plugin()
    path = tmp_path / "config.toml"
    original = '[plugin]\nenabled = true\nconfig_version = "1.0.0"\n[jm]\neh_site = "e"\n'
    path.write_text(original)
    monkeypatch.setattr(plugin_module, "__file__", str(tmp_path / "plugin.py"))
    with pytest.raises(ValueError):
        plugin._write_toml_sections({"jm": {"eh_site": "invalid"}})
    assert path.read_text() == original


def test_new_keys_stay_in_their_requested_section(tmp_path, monkeypatch):
    import tomllib
    plugin = make_plugin()
    path = tmp_path / "config.toml"
    path.write_text('[plugin]\nenabled = true\nconfig_version = "1.0.0"\n[jm]\nenabled = true\n')
    monkeypatch.setattr(plugin_module, "__file__", str(tmp_path / "plugin.py"))
    plugin._write_toml_sections({"plugin": {"config_version": "1.0.0"}, "jm": {"daily_count": 2}})
    assert tomllib.loads(path.read_text())["jm"]["daily_count"] == 2


def test_manual_recommendation_remains_one_even_when_schedule_count_is_higher():
    plugin = make_plugin()
    plugin.config.jm.daily_count = 5
    class Provider:
        prefix = "copy"
        async def search(self, *args):
            return [plugin_module.LookupResult(str(i), url=f"https://fixture/{i}") for i in range(6)]
    plugin._daily_providers = lambda *args: [Provider()]
    plugin._llm_pick = lambda *args: asyncio.sleep(0, result=0)
    assert len(asyncio.run(plugin._daily_build_items(1, "qq_group_123"))) == 1


def test_real_opaque_session_inherits_group_overrides_from_message():
    plugin = make_plugin()
    sid = "db95dfdad641eae0dbe747cec04df012"
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    plugin._session_overrides["123"] = {"adult": False, "send_cover": False}
    message = {"session_id": sid, "message_info": {"group_info": {"group_id": "123"}}}
    key, private = plugin._session_ctx({"stream_id": sid, "message": message})
    assert not private
    assert not plugin._adult_on(key, private)
    assert not plugin._effective_session_bool("send_cover", True, key, private)


def test_scheduled_opaque_group_is_not_misclassified_as_private():
    plugin = make_plugin()
    sid = "db95dfdad641eae0dbe747cec04df012"
    plugin.config.jm.daily_targets = ["123"]
    plugin.config.jm.allow_private = False
    plugin.config.jm.allowed_groups = ["123"]
    async def streams(**kwargs):
        return [{"session_id": sid, "group_id": "123", "chat_type": "group"}]
    plugin.ctx.chat = types.SimpleNamespace(get_all_streams=streams)
    assert asyncio.run(plugin._resolve_daily_targets()) == [sid]
    assert not plugin._target_is_private(sid)
    assert plugin._command_allowed({"stream_id": sid})
    plugin.config.jm.allowed_groups = ["456"]
    assert not plugin._command_allowed({"stream_id": sid})


def test_timed_out_blocking_calls_keep_their_concurrency_slots():
    import threading
    gate = threading.Event()
    started = []
    async def run():
        def work():
            started.append(1)
            gate.wait(2)
        try:
            outcomes = await asyncio.gather(*[plugin_module._run_blocking(work, 0.03) for _ in range(8)], return_exceptions=True)
            assert all(isinstance(result, TimeoutError) for result in outcomes)
            assert len(started) <= 4
        finally:
            gate.set()
    asyncio.run(run())


def test_actual_sdk_registers_commands_apis_and_hook():
    plugin = make_plugin()
    components = plugin.get_components()
    assert len(components) == 24


def test_webui_fields_match_backend_without_duplicates():
    import re
    html = (PLUGIN_PATH.parent / "webui/check.html").read_text()
    fields = re.findall(r"\['jm\.([a-z0-9_]+)'", html)
    assert len(fields) == len(set(fields))
    assert set(fields) | {"jm_domain", "copymanga_domain", "gmanga_domain"} == set(plugin_module.JmConfig.model_fields)


def test_cover_disabled_does_not_download_image():
    plugin = make_plugin()
    plugin.config.jm.send_cover = True
    plugin._session_overrides["123"] = {"send_cover": False}
    class Provider:
        prefix = "copy"
        async def cover(self, *args):
            raise AssertionError("disabled cover must not be downloaded")
    result = plugin_module.LookupResult("title", cover_url="https://fixture/cover")
    assert asyncio.run(plugin._prepare_cover_multi(Provider(), result, "tag", "qq_group_123")) is None


def test_text_only_daily_recommendation_can_use_chat_record():
    plugin = make_plugin()
    plugin.config.jm.daily_cover = False
    class Provider:
        prefix = "copy"
        async def detail(self, *args):
            return None
    seen = []
    async def forward(nodes, target, **kwargs):
        seen.append(nodes)
        return True
    plugin.ctx.send = types.SimpleNamespace(forward=forward)
    item = plugin_module.LookupResult("title", url="https://fixture/comic/slug")
    assert asyncio.run(plugin._send_daily_one("qq_group_123", Provider(), item, "q")) is True
    assert len(seen[0]) == 1


def test_private_jm_command_respects_no_cover_override():
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    plugin.config.jm.send_cover = True
    plugin.config.jm.private_send_cover = "off"
    plugin._lookup = lambda *args: asyncio.sleep(0, result=("title", "author"))
    async def no_download(*args):
        raise AssertionError("private off must prevent cover download")
    plugin._download_cover = no_download
    plugin._send = lambda *args: asyncio.sleep(0, result=True)
    result = asyncio.run(plugin.handle_jm_command(stream_id="qq_private_123", matched_groups={"id":"12345"}))
    assert result[0] is True


def test_slash_command_is_not_reprocessed_by_auto_detect_hook():
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    created = []
    class FakeTask:
        def __init__(self, coro):
            created.append(coro)
            coro.close()
        def add_done_callback(self, callback):
            return None
    def fake_task(coro):
        return FakeTask(coro)
    import asyncio as _asyncio
    original = _asyncio.create_task
    _asyncio.create_task = fake_task
    try:
        message = {
            "session_id": "db95dfdad641eae0dbe747cec04df012",
            "message_id": "1",
            "raw_message": [{"type": "text", "data": "/jm 123456"}],
            "processed_plain_text": "/jm 123456",
            "message_info": {"group_info": {"group_id": "123"}},
        }
        assert asyncio.run(plugin.on_message(message=message)) == {}
        assert created == []
    finally:
        _asyncio.create_task = original


def test_plain_prefix_message_is_still_auto_detected():
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    created = []
    class FakeTask:
        def __init__(self, coro):
            created.append(coro)
            coro.close()
        def add_done_callback(self, callback):
            return None
    def fake_task(coro):
        return FakeTask(coro)
    import asyncio as _asyncio
    original = _asyncio.create_task
    _asyncio.create_task = fake_task
    try:
        message = {
            "session_id": "db95dfdad641eae0dbe747cec04df012",
            "message_id": "2",
            "raw_message": [{"type": "text", "data": "JM123456"}],
            "processed_plain_text": "JM123456",
            "message_info": {"group_info": {"group_id": "123"}},
        }
        assert asyncio.run(plugin.on_message(message=message)) == {}
        assert len(created) == 1
    finally:
        _asyncio.create_task = original


@pytest.mark.parametrize("command_text", [
    "/jmcheck",
    "/jm 123456",
    "/nh 123456",
    "/eh 1234567/abcdef1234",
    "/搜漫画 斗罗",
    "/搜标签 yuri",
    "/copy naoodngxueshenghui",
    "/gm wuliandianfeng-pikapi",
    "/今日推荐",
    "/搜漫帮助",
])
def test_every_slash_command_is_left_to_command_layer(command_text):
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    created = []
    class FakeTask:
        def __init__(self, coro):
            created.append(coro)
            coro.close()
        def add_done_callback(self, callback):
            return None
    import asyncio as _asyncio
    original = _asyncio.create_task
    _asyncio.create_task = FakeTask
    try:
        message = {
            "session_id": "db95dfdad641eae0dbe747cec04df012",
            "message_id": "9",
            "raw_message": [{"type": "text", "data": command_text}],
            "processed_plain_text": command_text,
            "message_info": {"group_info": {"group_id": "123"}},
        }
        assert asyncio.run(plugin.on_message(message=message)) == {}
        assert created == [], f"{command_text} should not be auto-detected"
    finally:
        _asyncio.create_task = original


@pytest.mark.parametrize("plain_text,expected", [
    ("JM123456", 1),
    ("禁漫123456", 1),
    ("本子98765", 1),
    ("jM1234", 1),
    ("看看这个 123456", 0),
])
def test_plain_messages_still_auto_detect(plain_text, expected):
    plugin = make_plugin()
    plugin.config.jm.adult_declared = plugin.config.jm.adult_enabled = True
    created = []
    class FakeTask:
        def __init__(self, coro):
            created.append(coro)
            coro.close()
        def add_done_callback(self, callback):
            return None
    import asyncio as _asyncio
    original = _asyncio.create_task
    _asyncio.create_task = FakeTask
    try:
        message = {
            "session_id": "db95dfdad641eae0dbe747cec04df012",
            "message_id": "10",
            "raw_message": [{"type": "text", "data": plain_text}],
            "processed_plain_text": plain_text,
            "message_info": {"group_info": {"group_id": "123"}},
        }
        asyncio.run(plugin.on_message(message=message))
        assert len(created) == expected, plain_text
    finally:
        _asyncio.create_task = original


def test_mask_assets_selects_platform(monkeypatch):
    import platform as _platform
    plugin = make_plugin()
    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    monkeypatch.setattr(_platform, "machine", lambda: "aarch64")
    assert plugin._mask_assets() == ("jm-mask-rs-linux-aarch64", "runtime-linux-aarch64.tar.gz")
    monkeypatch.setattr(_platform, "machine", lambda: "x86_64")
    assert plugin._mask_assets() == ("jm-mask-rs-linux-x86_64", "runtime-linux-x86_64.tar.gz")
    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    assert plugin._mask_assets() is None


def test_mask_env_points_into_install_dir(tmp_path):
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    plugin.config.jm.ahm_api_url = "http://127.0.0.1:8123/process/"
    env = plugin._mask_env()
    assert env["MASK_ADDR"] == "127.0.0.1:8123"
    assert env["MASK_SEG_MODEL"] == str(tmp_path / "segmentation_model.onnx")
    assert env["MASK_CLS_MODEL"] == str(tmp_path / "classification_model.onnx")
    assert env["MASK_ORT_DYLIB"] == str(tmp_path / "libonnxruntime.so.1.32.0")


def test_mask_install_cleans_temp_files_on_failure(tmp_path, monkeypatch):
    import platform as _platform
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    monkeypatch.setattr(_platform, "machine", lambda: "aarch64")
    async def boom(*args, **kwargs):
        raise OSError("network down")
    monkeypatch.setattr(plugin, "_mask_download", boom)
    state = asyncio.run(plugin._mask_install())
    assert state.tone == "error"
    assert list(tmp_path.glob("*.part")) == []


def test_mask_start_skips_when_port_already_serving(tmp_path, monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    (tmp_path / "jm-mask-rs").write_text("#!/bin/sh\n")
    (tmp_path / "jm-mask-rs").chmod(0o755)
    async def in_use():
        return True
    monkeypatch.setattr(plugin, "_mask_port_in_use", in_use)
    state = asyncio.run(plugin._mask_start())
    assert state.tone == "neutral"
    assert not plugin._mask_running()


def test_mask_port_in_use_detects_listening_socket():
    import socket
    plugin = make_plugin()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    plugin.config.jm.ahm_api_url = f"http://127.0.0.1:{port}/process/"
    try:
        assert asyncio.run(plugin._mask_port_in_use()) is True
    finally:
        srv.close()
    assert asyncio.run(plugin._mask_port_in_use()) is False


def test_ensure_skips_when_port_already_serving(tmp_path, monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    calls = []
    async def in_use():
        return True
    async def install():
        calls.append("install")
        raise AssertionError("should not install")
    monkeypatch.setattr(plugin, "_mask_port_in_use", in_use)
    monkeypatch.setattr(plugin, "_mask_install", install)
    asyncio.run(plugin._mask_ensure())
    assert calls == []


def test_ensure_auto_installs_when_missing(tmp_path, monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    plugin.config.jm.ahm_auto_install = True
    plugin.config.jm.ahm_manage_process = True
    calls = []
    async def not_in_use():
        return False
    async def install():
        calls.append("install")
        (tmp_path / "jm-mask-rs").write_text("#!/bin/sh\n")
        (tmp_path / "jm-mask-rs").chmod(0o755)
        return plugin_module.ConfigWidgetState(text="installed", tone="success")
    async def start():
        calls.append("start")
        return plugin_module.ConfigWidgetState(text="running", tone="success")
    monkeypatch.setattr(plugin, "_mask_port_in_use", not_in_use)
    monkeypatch.setattr(plugin, "_mask_install", install)
    monkeypatch.setattr(plugin, "_mask_start", start)
    asyncio.run(plugin._mask_ensure())
    assert calls == ["install", "start"]


def test_ensure_respects_disabled_auto_install(tmp_path, monkeypatch):
    plugin = make_plugin()
    plugin.config.jm.mask_install_dir = str(tmp_path)
    plugin.config.jm.ahm_auto_install = False
    calls = []
    async def not_in_use():
        return False
    async def install():
        calls.append("install")
        return plugin_module.ConfigWidgetState(text="x", tone="success")
    monkeypatch.setattr(plugin, "_mask_port_in_use", not_in_use)
    monkeypatch.setattr(plugin, "_mask_install", install)
    asyncio.run(plugin._mask_ensure())
    assert calls == []
