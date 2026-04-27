"""Tests for voxtera.rag.embeddings — OpenAI embedding wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import openai
import pytest

from voxtera.rag.embeddings import _BATCH_SIZE, EMBEDDING_DIM, embed

# ---------------------------------------------------------------------------
# Helpers to build mock responses
# ---------------------------------------------------------------------------

@dataclass
class _FakeEmbeddingItem:
    index: int
    embedding: list[float]
    object: str = "embedding"


@dataclass
class _FakeUsage:
    prompt_tokens: int = 10
    total_tokens: int = 10


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]
    model: str = "text-embedding-3-small"
    object: str = "list"
    usage: _FakeUsage | None = None

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = _FakeUsage()


def _make_response(count: int) -> _FakeEmbeddingResponse:
    """Build a fake embedding response for *count* inputs."""
    return _FakeEmbeddingResponse(
        data=[
            _FakeEmbeddingItem(index=i, embedding=[0.1] * EMBEDDING_DIM)
            for i in range(count)
        ]
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyInput:
    async def test_returns_empty_list(self) -> None:
        result = await embed([], api_key="fake")
        assert result == []


class TestSingleInput:
    async def test_returns_one_vector(self) -> None:
        mock_create = AsyncMock(return_value=_make_response(1))
        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = mock_create
            result = await embed(["hello"], api_key="fake")

        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM
        mock_create.assert_awaited_once()


class TestBatching:
    async def test_250_inputs_produce_three_batches(self) -> None:
        """250 texts should result in 3 API calls (100 + 100 + 50)."""
        call_sizes: list[int] = []

        async def _fake_create(*, model: str, input: list[str]) -> _FakeEmbeddingResponse:
            call_sizes.append(len(input))
            return _make_response(len(input))

        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = _fake_create
            result = await embed(["text"] * 250, api_key="fake")

        assert call_sizes == [100, 100, 50]
        assert len(result) == 250

    async def test_exact_batch_boundary(self) -> None:
        """Exactly 100 texts → 1 API call."""
        call_count = 0

        async def _fake_create(*, model: str, input: list[str]) -> _FakeEmbeddingResponse:
            nonlocal call_count
            call_count += 1
            return _make_response(len(input))

        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = _fake_create
            result = await embed(["text"] * _BATCH_SIZE, api_key="fake")

        assert call_count == 1
        assert len(result) == _BATCH_SIZE


class TestRetry:
    async def test_retries_on_5xx(self) -> None:
        """First call 500s, second succeeds → returns result."""
        attempts = 0

        async def _flaky_create(*, model: str, input: list[str]) -> _FakeEmbeddingResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise openai.APIStatusError(
                    message="Internal Server Error",
                    response=_FakeHTTPResponse(500),
                    body=None,
                )
            return _make_response(len(input))

        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = _flaky_create
            with patch("voxtera.rag.embeddings.asyncio.sleep", new_callable=AsyncMock):
                result = await embed(["hello"], api_key="fake")

        assert len(result) == 1
        assert attempts == 2

    async def test_no_retry_on_400(self) -> None:
        """4xx errors are raised immediately, no retry."""
        async def _bad_request(*, model: str, input: list[str]) -> _FakeEmbeddingResponse:
            raise openai.APIStatusError(
                message="Bad Request",
                response=_FakeHTTPResponse(400),
                body=None,
            )

        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = _bad_request
            with pytest.raises(openai.APIStatusError) as exc_info:
                await embed(["hello"], api_key="fake")
            assert exc_info.value.status_code == 400

    async def test_exhausts_retries_then_raises(self) -> None:
        """If all 3 retries fail with 5xx, the last exception is raised."""
        async def _always_500(*, model: str, input: list[str]) -> _FakeEmbeddingResponse:
            raise openai.APIStatusError(
                message="Internal Server Error",
                response=_FakeHTTPResponse(500),
                body=None,
            )

        with patch("voxtera.rag.embeddings.openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value.embeddings.create = _always_500
            with (
                patch("voxtera.rag.embeddings.asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(openai.APIStatusError),
            ):
                await embed(["hello"], api_key="fake")


# ---------------------------------------------------------------------------
# Minimal fake for httpx.Response that openai.APIStatusError expects
# ---------------------------------------------------------------------------

class _FakeHTTPRequest:
    """Minimal stand-in for httpx.Request."""

    def __init__(self) -> None:
        self.method = "POST"
        self.url = "https://api.openai.com/v1/embeddings"
        self.headers: dict[str, str] = {}


class _FakeHTTPResponse:
    """Minimal stand-in for httpx.Response used by openai.APIStatusError."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = ""
        self.request = _FakeHTTPRequest()

    def json(self) -> dict[str, str]:
        return {}

    @property
    def is_closed(self) -> bool:
        return True

    def read(self) -> bytes:
        return b""
