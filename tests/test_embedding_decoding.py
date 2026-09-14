import asyncio
import base64
import json
from pathlib import Path
import struct
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest

from nycti.llm.client import OpenAIClient, _decode_embedding


class EmbeddingDecodingTests(unittest.TestCase):
    def test_decodes_little_endian_float32_without_changing_dimensions(self):
        values = [0.25, -0.5, 0.0, 1.5] * 768
        payload = base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode()
        self.assertEqual(values, _decode_embedding(payload))

    def test_accepts_compatible_numeric_arrays(self):
        self.assertEqual([0.25, -0.5, 1.0], _decode_embedding([0.25, -0.5, 1]))

    def test_rejects_malformed_empty_or_nonfinite_vectors(self):
        invalid = [None, {}, [], "", "not base64!", "non-ascii \u00e9", [True], ["0.5"],
                   [None], [float("nan")], [float("inf")], [float("-inf")],
                   base64.b64encode(b"abc").decode(),
                   base64.b64encode(struct.pack("<f", float("nan"))).decode(),
                   base64.b64encode(struct.pack("<f", float("inf"))).decode()]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _decode_embedding(value)

    def test_embedding_request_preserves_usage_and_supports_both_formats(self):
        async def check():
            payload = base64.b64encode(struct.pack("<3f", 0.25, -0.5, 0.75)).decode()
            for embedding in (payload, [0.25, -0.5, 0.75]):
                calls = []

                async def create(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(
                        data=[SimpleNamespace(embedding=embedding)],
                        usage=SimpleNamespace(prompt_tokens=12, total_tokens=12),
                    )

                client = object.__new__(OpenAIClient)
                client.embedding_client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
                result = await client.create_embedding(model="test", feature="memory_retrieve_embed", text=" hello ")
                self.assertEqual([0.25, -0.5, 0.75], result.embedding)
                self.assertEqual(12, result.usage.prompt_tokens)
                self.assertEqual(12, result.usage.total_tokens)
                self.assertEqual("memory_retrieve_embed", result.usage.feature)
                self.assertEqual([{"model": "test", "input": "hello", "encoding_format": "base64"}], calls)

        asyncio.run(check())

    def test_empty_provider_data_is_an_explicit_error(self):
        async def create(**kwargs):
            return SimpleNamespace(data=[], usage=None)

        client = object.__new__(OpenAIClient)
        client.embedding_client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        with self.assertRaisesRegex(ValueError, "No embedding data"):
            asyncio.run(client.create_embedding(model="test", feature="test", text="hello"))

    def test_real_sdk_mocked_response_does_not_import_numpy(self):
        # A fresh interpreter avoids suite-wide SDK shims and pre-imported NumPy masking this regression.
        script = textwrap.dedent("""
            import asyncio, base64, builtins, json, socket, struct, sys
            from types import SimpleNamespace
            def no_network(*args, **kwargs):
                raise AssertionError("Unexpected network access")
            socket.socket.connect = no_network
            socket.create_connection = no_network
            real_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name.split('.')[0] == 'numpy':
                    raise AssertionError("Embedding decoding imported NumPy")
                return real_import(name, *args, **kwargs)
            builtins.__import__ = guarded_import
            import httpx
            from openai import AsyncOpenAI
            from nycti.llm.client import OpenAIClient
            calls = []
            payload = base64.b64encode(struct.pack('<3f', 0.25, -0.5, 0.75)).decode()
            def respond(request):
                body = json.loads(request.content)
                assert body['encoding_format'] == 'base64'
                calls.append(body)
                return httpx.Response(200, json={
                    'object': 'list', 'model': 'offline',
                    'data': [{'object': 'embedding', 'index': 0,
                              'embedding': payload if len(calls) == 1 else [0.25, -0.5, 0.75]}],
                    'usage': {'prompt_tokens': 1, 'total_tokens': 1},
                })
            def factory(**kwargs):
                return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
            async def run():
                settings = SimpleNamespace(openai_api_key='offline', openai_base_url='https://example.invalid/v1',
                                           openai_embedding_api_key=None, openai_embedding_base_url=None)
                client = OpenAIClient(settings, client_factory=factory)
                try:
                    for _ in range(2):
                        result = await client.create_embedding(model='offline', feature='test', text='hello')
                        assert result.embedding == [0.25, -0.5, 0.75]
                        assert result.usage.total_tokens == 1
                    assert 'numpy' not in sys.modules
                finally:
                    await client.client.close()
                    await client.embedding_client.close()
            asyncio.run(run())
            print(json.dumps({'calls': len(calls), 'numpy_loaded': 'numpy' in sys.modules}))
        """)
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"calls": 2, "numpy_loaded": False}, json.loads(completed.stdout))
