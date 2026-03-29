import asyncio

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.config import Platform, PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _StreamAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.sent = []
        self.edits = []
        self._edit_attempts = 0

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="stream-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self._edit_attempts += 1
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": content,
            }
        )
        if self._edit_attempts == 1:
            return SendResult(
                success=False,
                message_id=message_id,
                error=(
                    "503 Service Unavailable (error code: 0): upstream connect error "
                    "or disconnect/reset before headers"
                ),
            )
        return SendResult(success=True, message_id=message_id)


@pytest.mark.asyncio
async def test_stream_consumer_keeps_single_message_on_transient_edit_failure():
    adapter = _StreamAdapter()
    consumer = GatewayStreamConsumer(
        adapter=adapter,
        chat_id="1234",
        config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
    )

    task = asyncio.create_task(consumer.run())
    consumer.on_delta("hello")
    await asyncio.sleep(0.05)
    consumer.on_delta(" world")
    await asyncio.sleep(0.05)
    consumer.finish()
    await task

    assert len(adapter.sent) == 1
    assert adapter._edit_attempts >= 2
    assert adapter.edits[-1]["content"] == "hello world"
