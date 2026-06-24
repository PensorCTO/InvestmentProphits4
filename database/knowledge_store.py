"""Knowledge Core — episodic vector memory for semantic toxicity filtering."""

import asyncio
import logging
import os
from pathlib import Path

import aiohttp
import libsql
import numpy as np
import requests
from dotenv import load_dotenv

from database.arena_lock import arena_lock

load_dotenv()
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ARENA_LOCK_PATH = _PROJECT_ROOT / ".arena_db.lock"

EMBED_BATCH_CONCURRENCY = 12
EMBED_TIMEOUT_SECONDS = float(os.getenv("EMBED_TIMEOUT_SECONDS", "30"))
DEFAULT_EMBEDDING_MODEL = "hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:F16"
DEFAULT_EMBEDDING_DIMS = 1024
DEFAULT_QUERY_INSTRUCT = (
    "Given a paper trade entry context, retrieve similar historical trade setups"
)
RAG_COLD_START_MIN_VECTORS = 100


class KnowledgeStore:
    def __init__(self):
        self.replica_path = os.getenv("LOCAL_REPLICA_PATH", "./ip4_local_replica.db")
        self.sync_url = os.getenv("TURSO_DATABASE_URL")
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN")
        self.embedding_dims = int(
            os.getenv("EMBEDDING_DIMS", str(DEFAULT_EMBEDDING_DIMS))
        )
        self.ollama_url = os.getenv(
            "OLLAMA_URL", "http://localhost:11434/api/embed"
        )
        self.model_name = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        self.query_instruct = os.getenv(
            "EMBEDDING_QUERY_INSTRUCT", DEFAULT_QUERY_INSTRUCT
        )
        self._use_legacy_embeddings_api = self.ollama_url.rstrip("/").endswith(
            "/api/embeddings"
        )

    def get_client(self):
        from database.replica_store import open_replica

        return open_replica()

    def count_swarm_vectors(self, conn=None) -> int:
        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            return own_conn.execute(
                "SELECT COUNT(*) FROM knowledge_core_vectors"
            ).fetchone()[0]
        finally:
            if close_conn:
                own_conn.close()

    def count_lane_vectors(self, conn=None, *, shared: bool = True) -> int:
        """Count vectors in the corpus a lane actually queries for toxicity."""
        table = "knowledge_core_vectors" if shared else "prime_knowledge_vectors"
        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            return own_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            if close_conn:
                own_conn.close()

    def _format_embedding_prompt(self, text: str, is_query: bool) -> str:
        """Qwen3-Embedding instruct format for queries; plain text for documents."""
        if is_query:
            return f"Instruct: {self.query_instruct}\nQuery:{text}"
        return text

    def _parse_embedding_response(self, body: dict) -> list[float] | None:
        if "embeddings" in body and body["embeddings"]:
            return body["embeddings"][0]
        if "embedding" in body and body["embedding"]:
            return body["embedding"]
        return None

    def _build_embed_payload(self, text: str) -> dict:
        if self._use_legacy_embeddings_api:
            return {"model": self.model_name, "prompt": text}
        payload: dict = {
            "model": self.model_name,
            "input": text,
            "truncate": True,
        }
        if self.embedding_dims > 0:
            payload["dimensions"] = self.embedding_dims
        return payload

    def _normalize_embedding(self, raw_embedding: list[float] | None) -> list[float]:
        if not raw_embedding:
            return self._zero_embedding()
        if len(raw_embedding) > self.embedding_dims:
            raw_embedding = raw_embedding[: self.embedding_dims]
        if len(raw_embedding) != self.embedding_dims:
            logger.error(
                "Embedding dimension mismatch: expected %d, got %d",
                self.embedding_dims,
                len(raw_embedding),
            )
            return self._zero_embedding()
        vec = np.array(raw_embedding, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm == 0:
            return self._zero_embedding()
        return (vec / norm).tolist()

    def _zero_embedding(self) -> list[float]:
        return [0.0] * self.embedding_dims

    def _generate_real_embedding(
        self, text: str, is_query: bool = False
    ) -> list[float]:
        """Local Ollama embeddings via Qwen3-Embedding-0.6B-GGUF (/api/embed)."""
        formatted_text = self._format_embedding_prompt(text, is_query)
        payload = self._build_embed_payload(formatted_text)

        try:
            response = requests.post(
                self.ollama_url, json=payload, timeout=EMBED_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            return self._normalize_embedding(
                self._parse_embedding_response(response.json())
            )

        except requests.exceptions.Timeout:
            logger.error(
                "Ollama embedding timeout. Ensure local Ollama is running with %s.",
                self.model_name,
            )
            return self._zero_embedding()
        except Exception as e:
            logger.error("Ollama embedding failure: %s", e)
            return self._zero_embedding()

    async def _fetch_embedding_async(
        self,
        session: aiohttp.ClientSession,
        text: str,
        *,
        is_query: bool,
    ) -> list[float]:
        formatted_text = self._format_embedding_prompt(text, is_query)
        payload = self._build_embed_payload(formatted_text)
        try:
            async with session.post(
                self.ollama_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=EMBED_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                body = await response.json()
                return self._normalize_embedding(self._parse_embedding_response(body))
        except asyncio.TimeoutError:
            logger.error(
                "Ollama embedding timeout (async). Ensure local Ollama is running with %s.",
                self.model_name,
            )
            return self._zero_embedding()
        except Exception as e:
            logger.error("Ollama embedding failure (async): %s", e)
            return self._zero_embedding()

    async def embed_contexts_batch(
        self,
        contexts: list[str],
        *,
        is_query: bool = True,
        concurrency: int = EMBED_BATCH_CONCURRENCY,
    ) -> dict[str, list[float]]:
        """Concurrent Ollama embeds for unique context strings (lock-free I/O)."""
        if not contexts:
            return {}

        unique_contexts = list(dict.fromkeys(contexts))
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, list[float]] = {}

        async with aiohttp.ClientSession() as session:

            async def embed_one(context: str) -> tuple[str, list[float]]:
                async with semaphore:
                    embedding = await self._fetch_embedding_async(
                        session, context, is_query=is_query
                    )
                    return context, embedding

            pairs = await asyncio.gather(*(embed_one(ctx) for ctx in unique_contexts))

        for context, embedding in pairs:
            results[context] = embedding
        return results

    def serialize_vector(self, vector: list[float]) -> bytes:
        """Converts float list to F32_BLOB format for Turso/libSQL."""
        return np.array(vector, dtype=np.float32).tobytes()

    @staticmethod
    def format_entry_context(
        category: str,
        market_mid: float,
        fair_value: float,
        liquidity_tier: str,
        direction: str,
        market_id: str | None = None,
    ) -> str:
        base = (
            f"Evaluating {direction} on {category} market at mid {market_mid:.4f}, "
            f"fair {fair_value:.4f}, tier {liquidity_tier}"
        )
        return f"{base}, id {market_id}" if market_id else base

    @staticmethod
    def synthesize_legacy_entry_context(
        category: str,
        market_mid: float,
        entry_price: float,
        liquidity_tier: str,
        direction: str,
        market_id: str,
    ) -> str:
        """Best-effort PIT substitute for trades filled before entry_context existed."""
        return KnowledgeStore.format_entry_context(
            category,
            market_mid,
            entry_price,
            liquidity_tier,
            direction,
            market_id=market_id,
        )

    def backfill_missing_entry_context(self) -> int:
        """Populate entry_context on legacy trades using ledger join data."""
        with arena_lock(_ARENA_LOCK_PATH):
            return self._backfill_missing_entry_context_locked()

    def _backfill_missing_entry_context_locked(self) -> int:
        conn = self.get_client()
        updated = 0
        try:
            rows = conn.execute(
                """
                SELECT t.trade_id, t.market_id, t.direction, t.entry_price,
                       m.category, m.market_mid, m.liquidity_tier
                FROM trade_execution t
                JOIN markets_ledger m ON t.market_id = m.market_id
                WHERE t.entry_context IS NULL OR t.entry_context = ''
                """
            ).fetchall()
            for (
                trade_id,
                market_id,
                direction,
                entry_price,
                category,
                market_mid,
                liquidity_tier,
            ) in rows:
                entry_context = self.synthesize_legacy_entry_context(
                    category,
                    float(market_mid),
                    float(entry_price),
                    liquidity_tier,
                    direction,
                    market_id,
                )
                conn.execute(
                    """
                    UPDATE trade_execution
                    SET entry_context = ?
                    WHERE trade_id = ?
                    """,
                    (entry_context, trade_id),
                )
                updated += 1
            if updated:
                conn.commit()
                try:
                    conn.sync()
                except Exception as sync_e:
                    logger.warning("Cloud sync delayed during entry_context backfill: %s", sync_e)
                logger.info(
                    "Backfilled entry_context on %d legacy trade(s)", updated
                )
        finally:
            conn.close()
        return updated

    @staticmethod
    def compute_predictive_value(pnl: float, kelly_size: float) -> float | None:
        """ROIC-normalized signal: clamp(PnL / kelly_size, -1, 1)."""
        if kelly_size <= 0:
            return None
        return max(-1.0, min(1.0, pnl / kelly_size))

    @staticmethod
    def derive_setup_category(category: str, raw_signals: dict) -> str:
        """Dominant overlay + market category for institutional memory."""
        if not raw_signals:
            return category or "unknown"
        dominant = max(
            ((k, abs(v)) for k, v in raw_signals.items()),
            key=lambda x: x[1],
            default=("none", 0),
        )[0]
        return f"{category}:{dominant}"

    def category_failure_rate(
        self,
        setup_category: str,
        *,
        as_of: str | None = None,
        conn=None,
        k: int = 20,
    ) -> float:
        """Fraction of similar setup_category trades with predictive_value < 0."""
        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            if as_of:
                rows = own_conn.execute(
                    """
                    SELECT predictive_value FROM knowledge_core_vectors kcv
                    JOIN trade_execution te ON kcv.trade_id = te.trade_id
                    WHERE kcv.setup_category = ?
                      AND COALESCE(te.closed_at, kcv.created_at) <= ?
                    ORDER BY kcv.created_at DESC LIMIT ?
                    """,
                    (setup_category, as_of, k),
                ).fetchall()
            else:
                rows = own_conn.execute(
                    """
                    SELECT predictive_value FROM knowledge_core_vectors
                    WHERE setup_category = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (setup_category, k),
                ).fetchall()
        except Exception:
            return 0.0
        finally:
            if close_conn:
                own_conn.close()

        if len(rows) < 5:
            return 0.0
        failures = sum(1 for (pv,) in rows if pv is not None and pv < 0)
        return failures / len(rows)

    def prepare_swarm_post_mortem(
        self,
        trade_id: str,
        category: str,
        pnl: float,
        kelly_size: float,
        entry_context: str,
        *,
        setup_category: str | None = None,
        raw_signals: dict | None = None,
    ) -> dict | None:
        """Build post-mortem payload (Ollama embed) without touching the replica."""
        predictive_value = self.compute_predictive_value(pnl, kelly_size)
        if predictive_value is None:
            logger.warning(
                "Skipping post-mortem for %s: invalid kelly_size %.2f",
                trade_id,
                kelly_size,
            )
            return None

        embedding = self._generate_real_embedding(entry_context, is_query=False)
        if sum(abs(v) for v in embedding) == 0:
            logger.warning("Ollama offline — skipping post-mortem for %s", trade_id)
            return None

        if setup_category is None:
            setup_category = self.derive_setup_category(
                category, raw_signals or {}
            )

        return {
            "trade_id": trade_id,
            "category": category,
            "predictive_value": predictive_value,
            "thesis_summary": entry_context,
            "embedding": embedding,
            "setup_category": setup_category,
        }

    def persist_swarm_post_mortem(self, conn, payload: dict) -> bool:
        """Write a prepared swarm post-mortem under an existing replica connection."""
        try:
            vector_blob = self.serialize_vector(payload["embedding"])
            insight_id = f"mem_{payload['trade_id']}"
            params_with_setup = (
                insight_id,
                payload["trade_id"],
                payload["category"],
                payload["predictive_value"],
                payload["thesis_summary"],
                vector_blob,
                payload["setup_category"],
            )
            params_legacy = params_with_setup[:-1]

            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_core_vectors
                    (insight_id, trade_id, category, predictive_value, thesis_summary,
                     thesis_embedding, setup_category)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    params_with_setup,
                )
            except Exception:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_core_vectors
                    (insight_id, trade_id, category, predictive_value, thesis_summary, thesis_embedding)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    params_legacy,
                )

            inserted = conn.execute("SELECT changes()").fetchone()[0]
            if inserted:
                logger.info(
                    "Memory forged for %s [PV: %.2f]",
                    payload["trade_id"],
                    payload["predictive_value"],
                )
            return bool(inserted)
        except Exception as e:
            logger.error("Failed to record post-mortem: %s", e)
            return False

    def record_post_mortem(
        self,
        trade_id: str,
        category: str,
        pnl: float,
        kelly_size: float,
        entry_context: str,
        *,
        setup_category: str | None = None,
        raw_signals: dict | None = None,
        conn=None,
    ) -> bool:
        """Translates a closed trade into a vector memory."""
        payload = self.prepare_swarm_post_mortem(
            trade_id,
            category,
            pnl,
            kelly_size,
            entry_context,
            setup_category=setup_category,
            raw_signals=raw_signals,
        )
        if payload is None:
            return False

        if conn is not None:
            return self.persist_swarm_post_mortem(conn, payload)

        with arena_lock(_ARENA_LOCK_PATH):
            own_conn = self.get_client()
            try:
                inserted = self.persist_swarm_post_mortem(own_conn, payload)
                if inserted:
                    own_conn.commit()
                    try:
                        own_conn.sync()
                    except Exception as sync_e:
                        logger.warning("Cloud sync delayed: %s", sync_e)
                return inserted
            finally:
                own_conn.close()

    def score_toxicity_from_embedding(
        self,
        embedding: list[float],
        k: int = 5,
        *,
        as_of: str | None = None,
        conn=None,
    ) -> float:
        """
        RAG toxicity from a precomputed query embedding (DB only, no HTTP).
        Negative return = historically profitable (safe).
        Positive return = historically toxic (lost capital).
        """
        if sum(abs(v) for v in embedding) == 0:
            logger.warning("Zero embedding. Failing open (0 toxicity).")
            return 0.0

        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            if as_of:
                count = own_conn.execute(
                    """
                    SELECT count(*)
                    FROM knowledge_core_vectors kcv
                    JOIN trade_execution te ON kcv.trade_id = te.trade_id
                    WHERE te.status LIKE 'CLOSED_%'
                      AND COALESCE(te.closed_at, kcv.created_at) <= ?
                    """,
                    (as_of,),
                ).fetchone()[0]
            else:
                count = own_conn.execute(
                    "SELECT count(*) FROM knowledge_core_vectors"
                ).fetchone()[0]
            if count < k:
                return 0.0

            vector_blob = self.serialize_vector(embedding)
            if as_of:
                query = """
                    SELECT kcv.predictive_value,
                           vector_distance_cos(kcv.thesis_embedding, ?) AS distance
                    FROM knowledge_core_vectors kcv
                    JOIN trade_execution te ON kcv.trade_id = te.trade_id
                    WHERE te.status LIKE 'CLOSED_%'
                      AND COALESCE(te.closed_at, kcv.created_at) <= ?
                    ORDER BY distance ASC
                    LIMIT ?
                """
                neighbors = own_conn.execute(
                    query, (vector_blob, as_of, k)
                ).fetchall()
            else:
                query = """
                    SELECT predictive_value,
                           vector_distance_cos(thesis_embedding, ?) AS distance
                    FROM knowledge_core_vectors
                    ORDER BY distance ASC
                    LIMIT ?
                """
                neighbors = own_conn.execute(query, (vector_blob, k)).fetchall()

            toxicity_score = 0.0
            for pv, distance in neighbors:
                similarity_weight = max(0, 1.0 - distance)
                toxicity_score += -pv * similarity_weight

            return toxicity_score / k
        except Exception as e:
            logger.error("Semantic scoring failed: %s", e)
            return 0.0
        finally:
            if close_conn:
                own_conn.close()

    def query_semantic_toxicity(
        self, current_context: str, k: int = 5, *, as_of: str | None = None
    ) -> float:
        """
        RAG query. Returns toxicity based on similar past setups.
        Negative return = historically profitable (safe).
        Positive return = historically toxic (lost capital).
        """
        conn = self.get_client()
        try:
            if as_of:
                count = conn.execute(
                    """
                    SELECT count(*)
                    FROM knowledge_core_vectors kcv
                    JOIN trade_execution te ON kcv.trade_id = te.trade_id
                    WHERE te.status LIKE 'CLOSED_%'
                      AND COALESCE(te.closed_at, kcv.created_at) <= ?
                    """,
                    (as_of,),
                ).fetchone()[0]
            else:
                count = conn.execute(
                    "SELECT count(*) FROM knowledge_core_vectors"
                ).fetchone()[0]
            if count < k:
                return 0.0
        finally:
            conn.close()

        embedding = self._generate_real_embedding(current_context, is_query=True)
        return self.score_toxicity_from_embedding(embedding, k=k, as_of=as_of)

    def query_prime_toxicity(
        self,
        market_id: str,
        current_context: str,
        k: int = 5,
        *,
        as_of: str | None = None,
        conn=None,
    ) -> float:
        """
        RAG query against Prime's isolated vector memory.
        Scoped to market_id when enough rows exist; otherwise global table.
        """
        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            time_clause = " AND created_at <= ?" if as_of else ""
            time_param = (as_of,) if as_of else ()

            market_count = own_conn.execute(
                f"""
                SELECT count(*) FROM prime_knowledge_vectors
                WHERE market_id = ?{time_clause}
                """,
                (market_id, *time_param),
            ).fetchone()[0]

            if market_count >= k:
                count = market_count
                scope_filter = f"WHERE market_id = ?{time_clause}"
                scope_params: tuple = (market_id, *time_param)
            else:
                count = own_conn.execute(
                    f"SELECT count(*) FROM prime_knowledge_vectors{'' if not as_of else ' WHERE created_at <= ?'}",
                    time_param if as_of else (),
                ).fetchone()[0]
                scope_filter = f"WHERE 1=1{time_clause}" if as_of else ""
                scope_params = time_param if as_of else ()

            if count < k:
                return 0.0

            embedding = self._generate_real_embedding(current_context, is_query=True)
            if sum(abs(v) for v in embedding) == 0:
                logger.warning("Ollama offline. Prime failing open (0 toxicity).")
                return 0.0

            vector_blob = self.serialize_vector(embedding)

            if scope_filter.startswith("WHERE market_id"):
                query = f"""
                    SELECT predictive_value,
                           vector_distance_cos(thesis_embedding, ?) AS distance
                    FROM prime_knowledge_vectors
                    {scope_filter}
                    ORDER BY distance ASC
                    LIMIT ?
                """
                params = (vector_blob, *scope_params, k)
            elif as_of:
                query = """
                    SELECT predictive_value,
                           vector_distance_cos(thesis_embedding, ?) AS distance
                    FROM prime_knowledge_vectors
                    WHERE created_at <= ?
                    ORDER BY distance ASC
                    LIMIT ?
                """
                params = (vector_blob, as_of, k)
            else:
                query = """
                    SELECT predictive_value,
                           vector_distance_cos(thesis_embedding, ?) AS distance
                    FROM prime_knowledge_vectors
                    ORDER BY distance ASC
                    LIMIT ?
                """
                params = (vector_blob, k)

            neighbors = own_conn.execute(query, params).fetchall()

            toxicity_score = 0.0
            for pv, distance in neighbors:
                similarity_weight = max(0, 1.0 - distance)
                toxicity_score += -pv * similarity_weight

            return toxicity_score / k
        except Exception as e:
            logger.error("Prime semantic query failed: %s", e)
            return 0.0
        finally:
            if close_conn:
                own_conn.close()

    def query_lane_toxicity(
        self,
        market_id: str,
        current_context: str,
        *,
        shared: bool,
        k: int = 5,
        as_of: str | None = None,
        conn=None,
    ) -> float:
        """Lane-aware toxicity. Shared lanes query the rich swarm corpus;
        otherwise fall back to Prime's isolated vector table."""
        if not shared:
            return self.query_prime_toxicity(
                market_id, current_context, k=k, as_of=as_of, conn=conn
            )

        own_conn = conn or self.get_client()
        close_conn = conn is None
        try:
            if as_of:
                count = own_conn.execute(
                    """
                    SELECT count(*)
                    FROM knowledge_core_vectors kcv
                    JOIN trade_execution te ON kcv.trade_id = te.trade_id
                    WHERE te.status LIKE 'CLOSED_%'
                      AND COALESCE(te.closed_at, kcv.created_at) <= ?
                    """,
                    (as_of,),
                ).fetchone()[0]
            else:
                count = own_conn.execute(
                    "SELECT count(*) FROM knowledge_core_vectors"
                ).fetchone()[0]
        finally:
            if close_conn:
                own_conn.close()

        if count < k:
            return 0.0

        embedding = self._generate_real_embedding(current_context, is_query=True)
        if sum(abs(v) for v in embedding) == 0:
            logger.warning("Ollama offline. Lane failing open (0 toxicity).")
            return 0.0
        return self.score_toxicity_from_embedding(
            embedding, k=k, as_of=as_of, conn=conn
        )

    def prepare_prime_post_mortem(
        self,
        market_id: str,
        pnl: float,
        kelly_size: float,
        entry_context: str,
        trade_id: str | None = None,
    ) -> dict | None:
        """Build Prime post-mortem payload without touching the replica."""
        predictive_value = self.compute_predictive_value(pnl, kelly_size)
        if predictive_value is None:
            logger.warning(
                "Skipping prime post-mortem for %s: invalid kelly_size %.2f",
                trade_id or market_id,
                kelly_size,
            )
            return None

        embedding = self._generate_real_embedding(entry_context, is_query=False)
        if sum(abs(v) for v in embedding) == 0:
            logger.warning(
                "Ollama offline — skipping prime post-mortem for %s", market_id
            )
            return None

        return {
            "market_id": market_id,
            "trade_id": trade_id,
            "predictive_value": predictive_value,
            "embedding": embedding,
        }

    def persist_prime_post_mortem(self, conn, payload: dict) -> bool:
        """Write a prepared Prime post-mortem under an existing replica connection."""
        try:
            vector_blob = self.serialize_vector(payload["embedding"])
            insight_id = f"prime_{payload['trade_id'] or payload['market_id']}"

            conn.execute(
                """
                INSERT OR IGNORE INTO prime_knowledge_vectors
                (insight_id, market_id, predictive_value, thesis_embedding)
                VALUES (?, ?, ?, ?)
                """,
                (
                    insight_id,
                    payload["market_id"],
                    payload["predictive_value"],
                    vector_blob,
                ),
            )

            inserted = conn.execute("SELECT changes()").fetchone()[0]
            if inserted:
                logger.info(
                    "Prime memory forged for %s [PV: %.2f]",
                    payload["market_id"],
                    payload["predictive_value"],
                )
            return bool(inserted)
        except Exception as e:
            logger.error("Failed to record prime post-mortem: %s", e)
            return False

    def record_prime_post_mortem(
        self,
        market_id: str,
        pnl: float,
        kelly_size: float,
        entry_context: str,
        trade_id: str | None = None,
        *,
        conn=None,
    ) -> bool:
        """Record closed Prime trade into prime_knowledge_vectors."""
        payload = self.prepare_prime_post_mortem(
            market_id, pnl, kelly_size, entry_context, trade_id=trade_id
        )
        if payload is None:
            return False

        if conn is not None:
            return self.persist_prime_post_mortem(conn, payload)

        with arena_lock(_ARENA_LOCK_PATH):
            own_conn = self.get_client()
            try:
                inserted = self.persist_prime_post_mortem(own_conn, payload)
                if inserted:
                    own_conn.commit()
                    try:
                        own_conn.sync()
                    except Exception as sync_e:
                        logger.warning("Cloud sync delayed: %s", sync_e)
                return inserted
            finally:
                own_conn.close()

    @staticmethod
    def _calculate_pnl(entry_price: float, exit_price: float, size: float) -> float:
        shares = size / entry_price
        gross_return = shares * exit_price
        return gross_return - size

    def backfill_closed_trades(self, *, batch_limit: int = 25) -> int:
        """Ingest CLOSED_* trades missing from knowledge_core_vectors."""
        with arena_lock(_ARENA_LOCK_PATH):
            self._backfill_missing_entry_context_locked()
            conn = self.get_client()
            try:
                rows = conn.execute(
                    """
                    SELECT t.trade_id, m.category, t.entry_price, t.exit_price,
                           t.kelly_size, t.status, t.entry_context
                    FROM trade_execution t
                    JOIN markets_ledger m ON t.market_id = m.market_id
                    WHERE t.status LIKE 'CLOSED_%'
                      AND t.agent_id NOT LIKE 'PRIME_%'
                      AND t.agent_id NOT LIKE 'BENCH_%'
                      AND t.entry_context IS NOT NULL
                      AND t.entry_context != ''
                      AND t.trade_id NOT IN (
                          SELECT trade_id FROM knowledge_core_vectors
                      )
                    LIMIT ?
                    """,
                    (batch_limit,),
                ).fetchall()
            finally:
                conn.close()

        prepared: list[dict] = []
        for (
            trade_id,
            category,
            entry_price,
            exit_price,
            kelly_size,
            status,
            entry_context,
        ) in rows:
            del status
            pnl = self._calculate_pnl(entry_price, exit_price, kelly_size)
            payload = self.prepare_swarm_post_mortem(
                trade_id=trade_id,
                category=category,
                pnl=pnl,
                kelly_size=kelly_size,
                entry_context=entry_context,
            )
            if payload is not None:
                prepared.append(payload)

        if not prepared:
            return 0

        ingested = 0
        with arena_lock(_ARENA_LOCK_PATH):
            conn = self.get_client()
            try:
                for payload in prepared:
                    if self.persist_swarm_post_mortem(conn, payload):
                        ingested += 1
                if ingested:
                    conn.commit()
                    try:
                        conn.sync()
                    except Exception as sync_e:
                        logger.warning("Cloud sync delayed: %s", sync_e)
            finally:
                conn.close()

        if ingested:
            logger.info(
                "Backfilled %d post-mortem memories from closed trades.", ingested
            )

        return ingested

    def backfill_prime_closed_trades(self) -> int:
        """Ingest CLOSED_* PRIME_APEX trades missing from prime_knowledge_vectors."""
        with arena_lock(_ARENA_LOCK_PATH):
            conn = self.get_client()
            try:
                rows = conn.execute(
                    """
                    SELECT t.trade_id, t.market_id, t.entry_price, t.exit_price,
                           t.kelly_size, t.entry_context
                    FROM trade_execution t
                    WHERE t.status LIKE 'CLOSED_%'
                      AND t.agent_id LIKE 'PRIME_%'
                      AND ('prime_' || t.trade_id) NOT IN (
                          SELECT insight_id FROM prime_knowledge_vectors
                      )
                    """
                ).fetchall()
            finally:
                conn.close()

        prepared: list[dict] = []
        skipped = 0
        for trade_id, market_id, entry_price, exit_price, kelly_size, entry_context in rows:
            if not entry_context:
                skipped += 1
                continue
            pnl = self._calculate_pnl(entry_price, exit_price, kelly_size)
            payload = self.prepare_prime_post_mortem(
                market_id=market_id,
                pnl=pnl,
                kelly_size=kelly_size,
                entry_context=entry_context,
                trade_id=trade_id,
            )
            if payload is not None:
                prepared.append(payload)

        if not prepared:
            return 0

        ingested = 0
        with arena_lock(_ARENA_LOCK_PATH):
            conn = self.get_client()
            try:
                for payload in prepared:
                    if self.persist_prime_post_mortem(conn, payload):
                        ingested += 1
                if ingested:
                    conn.commit()
                    try:
                        conn.sync()
                    except Exception as sync_e:
                        logger.warning("Cloud sync delayed: %s", sync_e)
            finally:
                conn.close()

        if skipped:
            logger.info(
                "Prime backfill skipped %d closed trades without entry_context", skipped
            )

        return ingested

    def purge_and_reingest_vectors(self) -> dict[str, int]:
        """Purge vector tables and re-backfill from closed trades (ROIC PV)."""
        from database.migrate_schema import purge_vector_tables

        with arena_lock(_ARENA_LOCK_PATH):
            conn = self.get_client()
            try:
                purge_vector_tables(conn)
                conn.commit()
                try:
                    conn.sync()
                except Exception as sync_e:
                    logger.warning("Cloud sync delayed during purge: %s", sync_e)
            finally:
                conn.close()

        swarm = self.backfill_closed_trades()
        prime = self.backfill_prime_closed_trades()
        return {"swarm_ingested": swarm, "prime_ingested": prime}
