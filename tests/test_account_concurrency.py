import asyncio
import time

import pytest

from doubao_work2api.accounts import Account, AccountPool
from doubao_work2api.errors import AccountBusyError, NoAvailableAccountError
from doubao_work2api.models import ChatCompletionRequest
from doubao_work2api.service import ChatService


class ControlledTransport:
    def __init__(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.closed = 0
        self.payloads = []

    async def stream(self, account, payload):
        self.payloads.append(payload)
        await self.started.put(account.id)
        try:
            await self.release.wait()
            yield {"_event": "CHUNK_DELTA", "text": "OK"}
        finally:
            self.closed += 1


def make_service(tmp_path, cap):
    pool = AccountPool(tmp_path / "accounts", account_max_concurrent=cap)
    pool._accounts = [Account(id="one", cookies={"sessionid": "synthetic"}, device_id="test", web_id="test")]
    transport = ControlledTransport()
    service = ChatService(pool, transport, model_key="unused", app_id="test", workspace_root=tmp_path / "work")
    return service, transport, pool


@pytest.mark.parametrize(("cap", "parallel"), [(0, 3), (1, 1), (2, 2), (10, 10)])
async def test_real_service_obeys_cap_without_serializing_at_account_lock(tmp_path, cap, parallel):
    service, transport, pool = make_service(tmp_path, cap)
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "synthetic"}])
    tasks = [asyncio.create_task(service.complete(request)) for _ in range(parallel)]
    try:
        for _ in tasks:
            assert await asyncio.wait_for(transport.started.get(), timeout=2) == "one"
        assert pool.accounts[0].in_flight == parallel
        if cap:
            with pytest.raises(AccountBusyError) as error:
                await service.complete(request)
            assert error.value.status_code == 429
            assert error.value.code == "account_concurrency_limit"
            assert "0s" not in str(error.value)
        identities = [payload["messages"][0]["local_message_id"] for payload in transport.payloads]
        assert len(set(identities)) == parallel
        transport.release.set()
        results = await asyncio.gather(*tasks)
        assert all(result["choices"][0]["message"]["content"] == "OK" for result in results)
        assert pool.accounts[0].in_flight == 0
    finally:
        transport.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancellation_releases_capacity(tmp_path):
    service, transport, pool = make_service(tmp_path, 1)
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "synthetic"}])
    task = asyncio.create_task(service.complete(request))
    await asyncio.wait_for(transport.started.get(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.accounts[0].in_flight == 0
    assert transport.closed == 1
    transport.release.set()
    assert (await service.complete(request))["choices"][0]["message"]["content"] == "OK"


async def test_stream_holds_capacity_until_closed(tmp_path):
    service, transport, pool = make_service(tmp_path, 1)
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "synthetic"}], stream=True)
    stream = service.stream(request)
    await anext(stream)  # Role event precedes account selection.
    next_chunk = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(transport.started.get(), timeout=2)
        transport.release.set()
        await next_chunk
        assert pool.accounts[0].in_flight == 1
        with pytest.raises(AccountBusyError):
            await pool.acquire()
    finally:
        transport.release.set()
        await asyncio.gather(next_chunk, return_exceptions=True)
        await stream.aclose()
    assert pool.accounts[0].in_flight == 0
    assert transport.closed == 1


async def test_admin_can_change_cap_during_active_requests_without_interrupting_them(tmp_path):
    import httpx
    from doubao_work2api.app import create_app
    from doubao_work2api.config import Settings
    config = Settings(api_key="synthetic-api", admin_password="synthetic-admin", data_dir=tmp_path / "data", accounts_dir=tmp_path / "accounts", account_max_concurrent=2)
    app = create_app(config)
    app.state.pool._accounts = [Account(id="one", cookies={"sessionid": "synthetic"}, device_id="test", web_id="test")]
    controlled = ControlledTransport()
    app.state.service.transport = controlled
    payload = {"messages": [{"role": "user", "content": "synthetic"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/admin/login", json={"password": "synthetic-admin"})).status_code == 200
        requests = [asyncio.create_task(client.post("/v1/chat/completions", headers={"Authorization": "Bearer synthetic-api"}, json=payload)) for _ in range(2)]
        try:
            for _ in requests:
                await asyncio.wait_for(controlled.started.get(), timeout=2)
            assert (await client.put("/admin/config", json={"account_max_concurrent": 1})).status_code == 200
            assert app.state.pool.accounts[0].in_flight == 2
            blocked = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer synthetic-api"}, json=payload)
            assert blocked.status_code == 429
            assert blocked.json()["error"]["code"] == "account_concurrency_limit"
            assert (await client.put("/admin/config", json={"account_max_concurrent": 3})).status_code == 200
            requests.append(asyncio.create_task(client.post("/v1/chat/completions", headers={"Authorization": "Bearer synthetic-api"}, json=payload)))
            await asyncio.wait_for(controlled.started.get(), timeout=2)
            assert app.state.pool.accounts[0].in_flight == 3
            controlled.release.set()
            assert all(response.status_code == 200 for response in await asyncio.gather(*requests))
        finally:
            controlled.release.set()
            await asyncio.gather(*requests, return_exceptions=True)
    assert app.state.pool.accounts[0].in_flight == 0
    assert create_app(config).state.pool.account_max_concurrent == 3


async def test_busy_account_is_skipped_and_allowlist_still_applies(tmp_path):
    pool = AccountPool(tmp_path, account_max_concurrent=1)
    one = Account(id="one", cookies={}, device_id="one", web_id="one", in_flight=1)
    two = Account(id="two", cookies={}, device_id="two", web_id="two")
    pool._accounts = [one, two]
    with pytest.raises(AccountBusyError):
        await pool.acquire(allowed_ids={"one"})
    assert await pool.acquire() is two
    await pool.release(two)
    pool.account_max_concurrent = 2
    assert await pool.acquire(allowed_ids={"one"}) is one
    assert one.in_flight == 2
    await pool.release(one)


async def test_subsecond_cooldown_is_rounded_up(tmp_path):
    pool = AccountPool(tmp_path)
    pool._accounts = [Account(id="one", cookies={}, device_id="test", web_id="test", cooldown_until=time.time() + 0.8)]
    with pytest.raises(NoAvailableAccountError) as error:
        await pool.acquire()
    assert error.value.code == "account_cooling_down"
    assert "about 1s" in str(error.value)


def test_success_preserves_error_from_same_clock_tick(tmp_path, monkeypatch):
    pool = AccountPool(tmp_path / "accounts")
    account = Account(id="one", cookies={}, device_id="test", web_id="test")
    pool._accounts = [account]
    now = 1_700_000_000.0
    monkeypatch.setattr("doubao_work2api.accounts.time.time", lambda: now)
    pool.mark_cooldown(account, "same-tick failure", "429")
    pool.mark_success(account, started_at=now)
    assert account.error_code == "429"
    assert account.last_error == "same-tick failure"
    assert account.last_error_at == now
    assert not account.available


@pytest.mark.parametrize("disable", [False, True])
async def test_concurrent_success_does_not_erase_later_failure(tmp_path, disable):
    service, transport, pool = make_service(tmp_path, 0)
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "synthetic"}])
    task = asyncio.create_task(service.complete(request))
    await asyncio.wait_for(transport.started.get(), timeout=2)
    account = pool.accounts[0]
    pool.mark_cooldown(account, "later request failed", "710022004" if disable else "429")
    if disable:
        await pool.set_enabled(account.id, False)
    transport.release.set()
    await task
    assert account.error_code == ("710022004" if disable else "429")
    assert account.enabled is (not disable)
    assert not account.available
    assert account.in_flight == 0
