import asyncio
import sys

import pytest

from forge_agent.tools import build_default_registry


@pytest.mark.asyncio
async def test_timeout_kills_process_before_delayed_side_effect(tmp_path) -> None:
    registry = build_default_registry(tmp_path, command_timeout_s=0.1)
    python = f'"{sys.executable}"'
    code = (
        "import time; time.sleep(1); "
        "open('survived.txt', 'w', encoding='utf-8').write('bad')"
    )

    result = await registry.call(
        "run_command",
        {"command": f'{python} -c "{code}"', "timeout_s": 0.1},
    )
    await asyncio.sleep(1.1)

    assert result.error_code == "timeout"
    assert not (tmp_path / "survived.txt").exists()


@pytest.mark.asyncio
async def test_cancellation_kills_process_before_delayed_side_effect(tmp_path) -> None:
    registry = build_default_registry(tmp_path)
    python = f'"{sys.executable}"'
    code = (
        "import time; time.sleep(1); "
        "open('survived-cancel.txt', 'w', encoding='utf-8').write('bad')"
    )

    task = asyncio.create_task(
        registry.call(
            "run_command",
            {"command": f'{python} -c "{code}"', "timeout_s": 5},
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)

    assert not (tmp_path / "survived-cancel.txt").exists()
